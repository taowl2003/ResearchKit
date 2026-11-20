(() => {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const token = fragment.get('token');
  let savedBearer = null;
  try { savedBearer = sessionStorage.getItem('researchkit-web-token'); } catch (_) {}
  if (token) {
    try { sessionStorage.setItem('researchkit-web-token', token); } catch (_) {}
    history.replaceState(null, '', location.pathname);
  }
  const bearer = token || savedBearer;
  const notice = document.querySelector('#notice'), output = document.querySelector('#output');
  let current = null, dirty = false, left = null, right = null, me = null;
  let datasetEpoch = 0, datasetProjectEpoch = 0, datasetReferenceEpoch = 0, derivedDatasetEpoch = 0;
  let projectEpoch = 0, distributionEpoch = 0;
  const projectState = {rows: [], query: '', includeArchived: false};
  const distributionState = {rows: [], query: ''};
  const assetListState = {rows: [], query: ''};
  const componentAccessState = {includeInactive: false, epoch: 0};
  const foldMemory = new Map();
  let submissionEpoch = 0;
  const submissionState = {cursor: null, loading: false, projects: new Map(), openProjects: new Set(), filters: null, error: null, append: false};
  const submissionHistories = new Map();
  const reviewState = {cursor: null, loading: false, projects: new Map(), histories: new Map(), historyCursors: new Map(), historyLoading: new Map(), query: '', epoch: 0};
  let reviewQueryTimer = null;
  const taskState = {manager: {items: [], epoch: 0, notice: null}, personal: {items: [], epoch: 0}};
  let assetListEpoch = 0, assetDetailEpoch = 0, assetFileEpoch = 0, selectedAssetId = null;
  const selectedDatasets = new Map();
  const selectedProjectDatasets = new Set();
  const derivedListState = {rows: [], cursor: null, loading: false};
  const personalDatasetVisibility = {hidden: new Set(), identity: null, showHidden: false};
  const datasetAttachState = {rows: [], projects: [], projectId: '', references: null, sourcePath: ''};
  const DATA_REFERENCE_LIMITS = {count: 128, fileBytes: 512 * 1024 * 1024, totalBytes: 2 * 1024 * 1024 * 1024};
  const text = (node, value) => { node.textContent = value; };
  const api = async (path, options = {}) => {
    const headers = new Headers(options.headers || {});
    headers.set('Authorization', `Bearer ${bearer || ''}`);
    if (options.body) headers.set('Content-Type', 'application/json');
    const res = await fetch(path, {...options, headers, credentials: 'omit'});
    const body = await res.json();
    if (!res.ok) { const error = new Error(body.error || `HTTP ${res.status}`); error.status = res.status; throw error; }
    return body;
  };
  const post = (path, body) => api(path, {method: 'POST', body: JSON.stringify(body)});
  const fetchFile = async path => {
    const response = await fetch(path, {headers: {'Authorization': `Bearer ${bearer || ''}`}, credentials: 'omit'});
    if (!response.ok) { let body = {}; try { body = await response.json(); } catch (_) {} throw new Error(body.error || `HTTP ${response.status}`); }
    return response;
  };
  const experiments = window.ResearchKitExperiments?.mount?.({
    api, post, fetchFile, uuid: () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`,
    foldDetails, getMe: () => me, report: value => text(output, value),
  });
  const card = () => { const node = document.createElement('article'); node.className = 'card'; return node; };
  const button = (label, action) => { const node = document.createElement('button'); text(node, label); node.onclick = action; return node; };
  const span = value => { const node = document.createElement('span'); text(node, value); return node; };
  const paragraph = (node, value, className = '') => { const p = document.createElement('p'); p.className = className; text(p, value); node.append(p); };
  const markdown = (node, value, className = '') => {
    if (window.ResearchKitMarkdown?.append) return window.ResearchKitMarkdown.append(node, value, {className, empty: '暂无说明。'});
    return paragraph(node, value || '暂无说明。', className);
  };
  const heading = (node, value) => { const h = document.createElement('h3'); text(h, value); node.append(h); };
  function actions(node) { const group = document.createElement('div'); group.className = 'actions'; node.append(group); return group; }
  function foldIdentity() {
    return `${me?.uid ?? 'unknown'}:${me?.username || 'unknown'}`;
  }
  function foldStorageKey(page, key) {
    return `researchkit-web-fold:${foldIdentity()}:${page}:${key}`;
  }
  function foldOpen(page, key) {
    const storageKey = foldStorageKey(page, key);
    try {
      const value = sessionStorage.getItem(storageKey);
      if (value === 'open' || value === 'closed') return value === 'open';
    } catch (_) {}
    return foldMemory.get(storageKey) === true;
  }
  function saveFoldOpen(page, key, open) {
    const storageKey = foldStorageKey(page, key); foldMemory.set(storageKey, Boolean(open));
    try { sessionStorage.setItem(storageKey, open ? 'open' : 'closed'); } catch (_) {}
  }
  function foldDetails(page, key, summaryText, content, className = '') {
    const details = document.createElement('details'), summary = document.createElement('summary');
    details.className = `fold-group${className ? ` ${className}` : ''}`;
    text(summary, summaryText); details.append(summary, content);
    details.open = foldOpen(page, key);
    details.ontoggle = () => saveFoldOpen(page, key, details.open);
    return details;
  }
  function foldControls(host, page, keys, rerender) {
    if (!keys.length) return;
    const controls = document.createElement('div'); controls.className = 'fold-controls';
    controls.append(button('全部展开', () => { keys.forEach(key => saveFoldOpen(page, key, true)); rerender(); }),
      button('全部收起', () => { keys.forEach(key => saveFoldOpen(page, key, false)); rerender(); }));
    host.append(controls);
  }
  function textMatch(query, ...values) {
    const needle = String(query || '').trim().toLocaleLowerCase('zh-CN');
    return !needle || values.some(value => String(value || '').toLocaleLowerCase('zh-CN').includes(needle));
  }
  function projectArchiveFilter() {
    try { return sessionStorage.getItem(`researchkit-web-project-archive:${foldIdentity()}`) === 'show'; } catch (_) { return false; }
  }
  function personalVisibilityIdentity() {
    return `${me?.uid ?? ''}:${me?.username || ''}`;
  }
  function datasetHidden(datasetId) {
    return personalDatasetVisibility.hidden.has(datasetId);
  }
  function clearHiddenDatasetSelections(datasetId) {
    selectedProjectDatasets.delete(datasetId);
    for (const [key, reference] of selectedDatasets) {
      if (reference?.dataset_id === datasetId) selectedDatasets.delete(key);
    }
  }
  async function setPersonalDatasetVisibility(datasetId, hidden) {
    const identity = personalVisibilityIdentity();
    await api('/api/personal-resource-visibility', {
      method: 'PUT', body: JSON.stringify({kind: 'dataset', ref: datasetId, hidden}),
    });
    if (identity !== personalVisibilityIdentity()) return false;
    if (hidden) {
      personalDatasetVisibility.hidden.add(datasetId);
      clearHiddenDatasetSelections(datasetId);
    } else personalDatasetVisibility.hidden.delete(datasetId);
    return true;
  }
  function renderPersonalDatasetVisibilityControl() {
    const host = document.querySelector('#dataset-visibility-controls');
    if (!host) return;
    host.replaceChildren();
    const label = document.createElement('label'); label.className = 'inline-check';
    const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = personalDatasetVisibility.showHidden;
    checkbox.onchange = () => {
      personalDatasetVisibility.showHidden = checkbox.checked;
      renderDatasets(datasetAttachState.rows, true);
      renderDerivedDatasets(derivedListState.rows);
    };
    label.append(checkbox, span('显示已移除数据'));
    host.append(label, span('移除只改变当前账号的显示偏好，不影响他人或已有项目。'));
  }
  function saveProjectArchiveFilter(value) {
    try { sessionStorage.setItem(`researchkit-web-project-archive:${foldIdentity()}`, value ? 'show' : 'hide'); } catch (_) {}
  }
  function fileList(row, files, open, fixed = false) {
    const main = actions(row), advanced = document.createElement('details');
    const summary = document.createElement('summary');
    text(summary, '高级信息：固定依赖与内部文件'); advanced.append(summary);
    const internal = actions(advanced);
    files.forEach(file => {
      const target = file.path.startsWith('.researchkit/') || file.path === 'task' ? internal : main;
      target.append(button((fixed ? '查看 ' : file.editable ? '编辑 ' : '只读 ') + file.path, () => open(file.path)));
    });
    if (internal.childElementCount) row.append(advanced);
  }
  function sourceLabel(project) {
    if (project.source === 'teacher-assigned') return '教师任务项目';
    if (project.source === 'teacher-unassigned') return '导入项目 · 可信教师包（未指派，不能标为任务）';
    return '自建项目';
  }
  async function templates() {
    const catalog = await api('/api/catalog'), select = document.querySelector('#template-ref');
    const blank = document.createElement('option'); blank.value = ''; text(blank, '空 Python 项目');
    select.replaceChildren(blank);
    (catalog.templates || []).forEach(template => {
      const option = document.createElement('option'); option.value = template.ref;
      text(option, `${template.name || template.ref} · ${template.version || template.ref}`); select.append(option);
    });
  }
  async function packageExport(submissionId, trigger) {
    trigger.disabled = true;
    try {
      const res = await fetch(`/api/submissions/${encodeURIComponent(submissionId)}/package`, {method: 'POST', headers: {'Authorization': `Bearer ${bearer || ''}`, 'Content-Type': 'application/json'}, body: '{}', credentials: 'omit'});
      if (!res.ok) { let problem = {}; try { problem = await res.json(); } catch (_) {} const error = new Error(problem.error || `HTTP ${res.status}`); error.status = res.status; throw error; }
      const url = URL.createObjectURL(await res.blob()), link = document.createElement('a');
      link.href = url; link.download = `researchkit-${submissionId}.zip`; link.hidden = true; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 0);
      text(output, '教师包已下载。该包仅用于 Web 导入；它不是旧 CLI 包的可信来源声明。');
    } catch (error) { text(output, error.message); }
    finally { trigger.disabled = false; }
  }
  function renderProjects() {
    const list = document.querySelector('#project-list'); if (!list) return;
    list.replaceChildren();
    const visible = projectState.rows.filter(project => textMatch(projectState.query, project.name, project.project_id, project.path, sourceLabel(project)));
    const groups = new Map();
    visible.forEach(project => {
      const source = sourceLabel(project);
      if (!groups.has(source)) groups.set(source, []);
      groups.get(source).push(project);
    });
    const groupKeys = [];
    groups.forEach((rows, source) => { groupKeys.push(`source:${source}`); rows.forEach(project => {
      groupKeys.push(`project:${project.project_id || project.name || 'unknown'}`);
      const task = taskState.personal.items.find(item => item.project_id === project.project_id); if (task) groupKeys.push(`task:${task.family_id || task.task_id}`);
    }); });
    foldControls(list, 'projects', groupKeys, renderProjects);
    if (!visible.length) { paragraph(list, projectState.rows.length ? '没有符合当前搜索条件的项目。' : '暂无项目。'); return; }
    groups.forEach((rows, source) => {
      const content = document.createElement('div'); content.className = 'fold-content cards';
      const taskGroups = new Map();
      rows.forEach(project => {
        const row = card();
        heading(row, project.name || project.project_id || '未命名项目');
        const task = taskState.personal.items.find(item => item.project_id === project.project_id);
        if (task) paragraph(row, `教师任务：${task.name} · 任务版本 ${task.task_version || 1}`, 'path');
        paragraph(row, `项目 ID：${project.project_id || '不可用'}`, 'path');
        paragraph(row, project.path || '项目目录不可用', 'path');
        if (project.archived) paragraph(row, '已归档：只在默认列表隐藏；项目目录和实验产物仍在，固定提交保留，可恢复后继续编辑和提交。');
        if (project.central_purged) paragraph(row, '平台中央历史项目已清理；当前保留工作区本地副本，可继续查看、编辑或删除本地副本。');
        if (!project.unavailable) {
          appendProjectResources(row, project);
          fileList(row, project.files || [], path => openWorkspaceFile(project.project_id, path));
          const controls = actions(row);
          controls.append(button('提交小版本', () => submit(project.project_id, 'minor')),
                          button('提交大版本', () => submit(project.project_id, 'major')));
          const experimentDetails = document.createElement('details'), experimentSummary = document.createElement('summary'), experimentHost = document.createElement('div');
          text(experimentSummary, 'IDE 实验方案与产物'); experimentDetails.append(experimentSummary, experimentHost);
          let loadedExperiments = false;
          experimentDetails.ontoggle = () => { if (experimentDetails.open && !loadedExperiments) { loadedExperiments = true; experiments?.renderProject(experimentHost, project); } };
          row.append(experimentDetails);
        }
        const archiveControls = actions(row);
        const archiveButton = button(project.archived ? '恢复项目' : '归档项目', () => archiveProject(project, !project.archived, archiveButton));
        archiveControls.append(archiveButton);
        if (project.project_id) {
          const deleteBtn = button('删除本地副本...', () => {
            const existingBox = row.children?.find?.(c => c.className === 'local-delete-confirm');
            if (existingBox) return;
            const confirmBox = document.createElement('div');
            confirmBox.className = 'local-delete-confirm';
            paragraph(confirmBox, '确认后会真实删除工作区本地目录及其中产物；不会自动删除平台保存的固定提交与资源授权。');
            const confirmActions = actions(confirmBox);
            const doDeleteBtn = button('确认删除本地副本', async () => {
              doDeleteBtn.disabled = true;
              try {
                await post(`/api/projects/${encodeURIComponent(project.project_id)}/delete-local`, {confirmation: '确认删除本地副本'});
                await Promise.all([projects(), tasks('personal')]);
                text(output, `已删除项目「${project.name || project.project_id}」的工作区本地副本；历史固定提交仍保留。`);
              } catch (error) {
                paragraph(confirmBox, `删除失败：${error.message}`, 'action-result error');
                doDeleteBtn.disabled = false;
              }
            });
            const cancelBtn = button('取消', () => {
              confirmBox.remove?.();
              if (Array.isArray(row.children)) row.children = row.children.filter(c => c !== confirmBox);
            });
            confirmActions.append(doDeleteBtn, cancelBtn);
            row.append(confirmBox);
          });
          archiveControls.append(deleteBtn);
        }
        const item = document.createElement('div'); item.append(row);
        const projectCard = foldDetails('projects', `project:${project.project_id || project.name || 'unknown'}`, `${project.archived ? '已归档 · ' : ''}${project.name || project.project_id || '未命名项目'} · ${project.project_id || '项目 ID 不可用'}`, item, 'project-fold');
        if (task) {
          const key = task.family_id || task.task_id;
          if (!taskGroups.has(key)) {
            const versions = document.createElement('div'); taskGroups.set(key, versions);
            content.append(foldDetails('projects', `task:${key}`, task.name || key, versions, 'project-task-group'));
          }
          taskGroups.get(key).append(projectCard);
        } else content.append(projectCard);
      });
      list.append(foldDetails('projects', `source:${source}`, `${source} · 当前显示 ${rows.length} 个项目`, content, 'project-source-group'));
    });
  }
  async function projects() {
    const epoch = ++projectEpoch;
    const rows = await api(`/api/projects${projectState.includeArchived ? '?include_archived=true' : ''}`);
    if (epoch !== projectEpoch) return;
    projectState.rows = Array.isArray(rows) ? rows : [];
    renderProjects();
  }
  async function archiveProject(project, archive, trigger) {
    if (!project?.project_id) return;
    if (trigger) trigger.disabled = true;
    try {
      await post(`/api/projects/${encodeURIComponent(project.project_id)}/${archive ? 'archive' : 'restore'}`, {});
      await projects();
      text(output, archive ? '项目已归档；其固定提交、分发和候选记录仍保留。' : '项目已恢复到工作区列表。');
    } catch (error) { text(output, error.message); }
    finally { if (trigger) trigger.disabled = false; }
  }
  function setEditor(name, file, readonly) {
    if (dirty && !confirm('当前文件有未保存编辑。确定放弃这些编辑并切换文件吗？')) return false;
    current = file; dirty = false;
    text(document.querySelector('#file-name'), name);
    text(document.querySelector('#editor-state'), readonly ? '固定提交内容或固定依赖，仅供查看。' : '与 IDE 共用同一文件；保存前会检查已发现的 IDE 修改。');
    const editor = document.querySelector('#editor'); editor.value = file.text || ''; editor.disabled = readonly;
    document.querySelector('#save').disabled = readonly;
    ['compare-left', 'compare-right'].forEach(id => { document.querySelector('#' + id).disabled = !file.submission_id; });
    document.querySelector('#compare').disabled = !(left && right && file.submission_id);
    document.querySelector('#baseline-compare').disabled = !(file.submission_id && (file.distribution_id || file.baseline_submission_id));
    updateCompareStatus();
    return true;
  }
  async function openWorkspaceFile(project, path) {
    try {
      const file = await api(`/api/projects/${encodeURIComponent(project)}/file?path=${encodeURIComponent(path)}`);
      setEditor(path, {...file, project_id: project}, !file.editable);
    } catch (error) { text(output, error.message); }
  }
  async function openSubmissionFile(submission, path, distributionId, baselineSubmissionId) {
    try {
      const file = await api(`/api/submissions/${encodeURIComponent(submission)}/file?path=${encodeURIComponent(path)}`);
      if (file.binary) throw new Error('二进制内容不能在 Web 编辑器显示。');
      setEditor(`${submission} · ${path}`, {...file, submission_id: submission, distribution_id: distributionId, baseline_submission_id: baselineSubmissionId}, true);
    } catch (error) { text(output, error.message); }
  }
  async function showSubmission(id) {
    try {
      const value = await api(`/api/submissions/${encodeURIComponent(id)}`), row = card();
      heading(row, `版本 ${value.version} · ${value.submission_id}`);
      paragraph(row, sourceLabel(value.project || {}));
      fileList(row, value.manifest.files || [], path => openSubmissionFile(value.submission_id, path, value.project?.distribution_id, value.project?.baseline_submission_id || value.baseline_submission_id), true);
      const experimentHost = document.createElement('div'); row.append(experimentHost); experiments?.renderSubmission(experimentHost, value.submission_id);
      output.replaceChildren(row);
    } catch (error) { text(output, error.message); }
  }
  function distributionForm(row, submission) {
    const form = document.createElement('form'); form.className = 'distribution-form';
    const label = document.createElement('label'); text(label, '目标服务器账号');
    const input = document.createElement('input'); input.required = true; input.maxLength = 32; label.append(input);
    const send = document.createElement('button'); send.type = 'submit'; text(send, '分发此固定版本');
    form.append(label, send);
    form.onsubmit = async event => {
      event.preventDefault(); send.disabled = true;
      try {
        const value = await post('/api/distributions', {submission_id: submission.submission_id, target_username: input.value.trim()});
        text(output, `已分发给 ${value.target_username}，基线版本 ${value.source_version}。重复分发同一版本不会创建第二份任务。`);
        await distributions();
      } catch (error) { text(output, error.message); }
      finally { send.disabled = false; }
    };
    row.append(form);
  }
  function appendProjectResources(row, project) {
    const source = project.project_yaml?.origin?.template_ref;
    paragraph(row, source ? `来源平台案例：${source}` : '来源案例：未标注平台案例；可能为自建、导入或审核测试副本。', 'path');
    if (project.baseline_submission_id) paragraph(row, `教师基线：${project.baseline_submission_id}`, 'path');
    const details = document.createElement('details'), summary = document.createElement('summary'), body = document.createElement('div');
    text(summary, '查看本项目所用算法与数据（清理前核对）'); details.append(summary, body); row.append(details);
    let loading = false, loaded = false;
    const load = async () => {
      if (loading) return; loading = true; body.replaceChildren(); paragraph(body, '正在核对项目依赖…');
      try {
        const info = await api(`/api/projects/${encodeURIComponent(project.project_id)}/resources`);
        body.replaceChildren();
        if (info.warning) paragraph(body, info.warning);
        heading(body, '算法：代码保存在本项目中');
        if (!info.algorithms?.length) paragraph(body, '没有在 project.yaml 中配置算法。');
        (info.algorithms || []).forEach(item => {
          paragraph(body, `${item.alias} · ${item.ref || '版本未标注'} · ${item.kind === 'platform_copy' ? '平台算法的项目内副本' : '本地算法'}${item.modified ? '（依赖锁检测到改动）' : ''}`);
          if (item.source_ref) paragraph(body, `平台来源版本：${item.source_ref}`, 'path');
          paragraph(body, `项目内路径：${item.path}`, 'path');
          paragraph(body, `参数：${JSON.stringify(item.parameters || {})}`, 'path');
        });
        paragraph(body, '删除平台案例或平台算法，不会删除已经复制进这个项目的代码。');
        heading(body, '数据：只保存引用，运行时读取共享文件');
        if (!info.datasets?.length) paragraph(body, '没有受控数据引用。项目内自行保存的小文件不属于此清单。');
        (info.datasets || []).forEach(item => {
          paragraph(body, `${item.name || '数据集'} · ${item.dataset_id}${item.registered === false ? '（已移除登记或暂不可见）' : ''}`, 'path');
          (item.files || []).forEach(file => paragraph(body, typeof file === 'string' ? file : `${file.path === '__researchkit_dataset_tree__' ? '完整目录（只读） · 清单' : file.relative_path || file.path || '完整目录'}${file.sha256 ? ` · sha256 ${file.sha256}` : ''}`, 'path'));
          if (item.manifest_sha256) paragraph(body, `目录清单摘要：${item.manifest_sha256}`, 'path');
        });
        paragraph(body, '网页移除数据登记会保留磁盘文件和已有读取授权；若管理员另外手工删除磁盘文件，引用它的项目会无法读取。此清单只列出项目声明的算法与受控数据，代码中自行访问的其他依赖不在其中。');
        actions(body).append(button('重新核对依赖', load)); loaded = true;
      } catch (error) { body.replaceChildren(); paragraph(body, `读取依赖失败：${error.message}`, 'action-result error'); actions(body).append(button('重试', load)); }
      finally { loading = false; }
    };
    details.ontoggle = () => { if (details.open && !loaded) load(); };
  }
  function submissionFilters() {
    const read = id => String(document.querySelector(id)?.value || '').trim();
    return {q: read('#submission-query'), kind: read('#submission-kind') || 'all', from_date: read('#submission-from-date'), to_date: read('#submission-to-date')};
  }
  function submissionProjectsPath(cursor, filters = submissionState.filters || submissionFilters()) {
    const query = new URLSearchParams();
    if (filters.q) query.set('q', filters.q);
    query.set('kind', ['all', 'major', 'minor'].includes(filters.kind) ? filters.kind : 'all');
    if (filters.from_date) query.set('from_date', filters.from_date);
    if (filters.to_date) query.set('to_date', filters.to_date);
    query.set('limit', '20');
    if (cursor) query.set('cursor', cursor);
    return `/api/submission-projects?${query.toString()}`;
  }
  function formatBeijingTime(value) {
    if (!value || Number.isNaN(Date.parse(value))) return '时间不可用';
    try { return new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false}).format(new Date(value)); }
    catch (_) { return String(value); }
  }
  function ownerName(project) {
    if (project?.owner_username) return project.owner_username;
    if (project?.owner_uid !== undefined && project?.owner_uid !== null) return `账号 #${project.owner_uid}`;
    return '账号不可用';
  }
  function updateCompareStatus() {
    const node = document.querySelector('#submission-compare-status');
    if (!node) return;
    const describe = item => item?.submission_id ? `${item.submission_id}${item.path ? ` · ${item.path}` : ''}` : '尚未选择';
    text(node, `当前比较：左侧 ${describe(left)}；右侧 ${describe(right)}。在“打开固定版本”后选择同一路径文件，再用编辑器按钮设为左右版本。`);
  }
  function submissionControls(row, value) {
    const controls = actions(row);
    controls.append(button('打开固定版本', () => showSubmission(value.submission_id)));
  }
  function renderSubmissionVersion(value, label) {
    const row = card(), project = value.project || {};
    heading(row, `${label || '版本'} · ${value.version || '未知'} · ${value.kind === 'major' ? '大版本' : '小版本'}`);
    paragraph(row, `所属账号：${ownerName(project)} · 提交时间（北京时间）：${formatBeijingTime(value.created_at)}`);
    paragraph(row, `固定提交：${value.submission_id || '不可用'} · 提交摘要：${value.content_sha256 || '不可用'}`, 'path');
    submissionControls(row, value);
    return row;
  }
  function renderSubmissionHistory(projectId, preview) {
    const state = submissionHistories.get(projectId), host = document.createElement('div');
    if (!state?.attempted) { paragraph(host, '展开后读取该项目的其余历史版本。'); return host; }
    (state?.loaded || []).filter(value => value.submission_id !== preview?.submission_id).forEach(value => host.append(renderSubmissionVersion(value, '历史版本')));
    if (state?.loading) paragraph(host, '正在读取历史版本…');
    if (state?.error) {
      paragraph(host, `读取历史版本失败：${state.error}`);
      host.append(button('重试读取历史版本', () => loadSubmissionHistory(projectId, preview, false, true)));
    }
    if (state?.cursor && !state.loading) host.append(button('更多历史版本', () => loadSubmissionHistory(projectId, preview, true)));
    if (state?.loaded?.length && !state?.cursor && !state.loading) paragraph(host, '该项目没有更多匹配的历史版本。');
    return host;
  }
  function renderSubmissionProjects() {
    const list = document.querySelector('#submission-list'), more = document.querySelector('#submission-more'), message = document.querySelector('#submission-message');
    if (!list || !more) return;
    list.replaceChildren(); more.replaceChildren();
    const groups = new Map();
    submissionState.projects.forEach(item => {
      const project = item?.project || {}, key = `${project.owner_uid ?? ''}\u0000${ownerName(project)}`;
      if (!groups.has(key)) groups.set(key, {name: ownerName(project), items: []});
      groups.get(key).items.push(item);
    });
    if (submissionState.error && !submissionState.projects.size) {
      text(message, `读取固定提交失败：${submissionState.error}`);
      list.append(button('重试读取固定提交', () => versions()));
    } else {
      text(message, submissionState.loading && !submissionState.projects.size ? '正在读取固定提交…' : '');
      const groupKeys = [...groups.keys()].map(key => `owner:${key}`);
      foldControls(list, 'submissions', groupKeys, renderSubmissionProjects);
      groups.forEach((group, ownerKey) => {
        const groupNode = document.createElement('div'); groupNode.className = 'submission-owner-group cards';
        group.items.forEach(item => {
          const project = item.project || {}, row = card(), preview = item.preview;
          heading(row, project.name || project.project_id || '未命名项目');
          paragraph(row, `项目 ID：${project.project_id || '不可用'} · ${sourceLabel(project)}`, 'path');
          paragraph(row, `最新匹配提交（北京时间）：${formatBeijingTime(item.latest_at)} · 匹配 ${Number.isSafeInteger(item.version_count) ? item.version_count : 0} 个版本`);
          const previewLabel = submissionState.filters?.kind === 'minor' ? '预览版本（仅小版本筛选）' : '预览版本（大版本优先）';
          if (preview?.submission_id) row.append(renderSubmissionVersion(preview, previewLabel));
          else paragraph(row, '服务未返回可打开的预览版本。');
          const details = document.createElement('details'), summary = document.createElement('summary');
          text(summary, `查看历史版本（匹配 ${Number.isSafeInteger(item.version_count) ? item.version_count : 0} 个）`); details.append(summary);
          const history = document.createElement('div'); history.className = 'submission-history'; history.append(renderSubmissionHistory(project.project_id, preview)); details.append(history);
          const historyKey = `history:${project.project_id}`;
          let restoring = foldOpen('submissions', historyKey) || submissionState.openProjects.has(project.project_id);
          details.ontoggle = () => {
            if (restoring) return;
            if (details.open) {
              submissionState.openProjects.add(project.project_id);
              const state = submissionHistories.get(project.project_id);
              if (!state?.attempted) loadSubmissionHistory(project.project_id, preview, false);
            }
            else submissionState.openProjects.delete(project.project_id);
            saveFoldOpen('submissions', historyKey, details.open);
          };
          details.open = restoring;
          if (restoring) setTimeout(() => { restoring = false; }, 0);
          row.append(details); groupNode.append(row);
        });
        const ownerDetails = foldDetails('submissions', `owner:${ownerKey}`, `所属账号：${group.name} · 当前已加载 ${group.items.length} 个项目`, groupNode, 'submission-owner-group');
        const saveOwnerToggle = ownerDetails.ontoggle, groupEpoch = submissionEpoch;
        const restoreVisibleHistory = () => {
          if (!ownerDetails.open || ownerDetails.isConnected === false || groupEpoch !== submissionEpoch) return;
          group.items.forEach(item => {
            const id = item.project?.project_id;
            if ((foldOpen('submissions', `history:${id}`) || submissionState.openProjects.has(id)) && !submissionHistories.get(id)?.attempted) {
              loadSubmissionHistory(id, item.preview, false);
            }
          });
        };
        ownerDetails.ontoggle = () => { saveOwnerToggle(); restoreVisibleHistory(); };
        list.append(ownerDetails);
        if (ownerDetails.open) setTimeout(restoreVisibleHistory, 0);
      });
      if (!submissionState.projects.size && !submissionState.loading) paragraph(list, '没有符合条件的固定提交。可调整筛选条件后重试。');
      if (submissionState.error && submissionState.projects.size) {
        const retry = button(`加载更多失败：${submissionState.error}。重试`, () => loadSubmissionProjects(true)); more.append(retry);
      } else if (submissionState.loading) paragraph(more, submissionState.append ? '正在加载更多项目…' : '正在读取固定提交…');
      else if (submissionState.cursor) more.append(button('加载更多项目', () => loadSubmissionProjects(true)));
      else if (submissionState.projects.size) paragraph(more, '已显示当前筛选条件下的全部项目。');
    }
  }
  async function loadSubmissionProjects(append, filters = append ? submissionState.filters : submissionFilters()) {
    if (append && (submissionState.loading || !submissionState.cursor)) return;
    const epoch = append ? submissionEpoch : ++submissionEpoch;
    if (!append) { submissionState.cursor = null; submissionState.projects.clear(); submissionState.filters = {...filters}; submissionState.error = null; }
    submissionState.loading = true; submissionState.append = Boolean(append); renderSubmissionProjects();
    try {
      const value = await api(submissionProjectsPath(append ? submissionState.cursor : null, filters));
      if (epoch !== submissionEpoch) return;
      if (!Array.isArray(value?.items)) throw new Error('服务返回的项目列表无效。');
      value.items.forEach(item => {
        const id = item?.project?.project_id;
        if (typeof id === 'string' && id) submissionState.projects.set(id, item);
      });
      submissionState.cursor = typeof value.next_cursor === 'string' && value.next_cursor ? value.next_cursor : null;
      submissionState.error = null;
    } catch (error) {
      if (epoch !== submissionEpoch) return;
      submissionState.error = error.message;
    } finally {
      if (epoch === submissionEpoch) { submissionState.loading = false; submissionState.append = false; renderSubmissionProjects(); }
    }
  }
  async function loadSubmissionHistory(projectId, preview, append, retry = false) {
    if (!projectId) return;
    let state = submissionHistories.get(projectId);
    if (!state) { state = {epoch: 0, cursor: null, loaded: [], loading: false, attempted: false, fetched: false, filters: {...(submissionState.filters || submissionFilters())}, error: null}; submissionHistories.set(projectId, state); }
    if (state.loading || (append && !state.cursor) || (!append && (state.fetched || (state.attempted && !retry)))) return;
    const epoch = ++state.epoch;
    state.loading = true; state.attempted = true; state.error = null; renderSubmissionProjects();
    try {
      const query = new URLSearchParams(), filters = state.filters;
      if (filters.q) query.set('q', filters.q);
      query.set('kind', ['all', 'major', 'minor'].includes(filters.kind) ? filters.kind : 'all');
      if (filters.from_date) query.set('from_date', filters.from_date);
      if (filters.to_date) query.set('to_date', filters.to_date);
      query.set('limit', '20'); if (append && state.cursor) query.set('cursor', state.cursor);
      const value = await api(`/api/submission-projects/${encodeURIComponent(projectId)}/versions?${query.toString()}`);
      if (state !== submissionHistories.get(projectId) || epoch !== state.epoch) return;
      if (!Array.isArray(value?.items)) throw new Error('服务返回的历史版本无效。');
      const known = new Set(state.loaded.map(item => item.submission_id));
      value.items.forEach(item => { if (item?.submission_id && !known.has(item.submission_id)) { known.add(item.submission_id); state.loaded.push(item); } });
      state.cursor = typeof value.next_cursor === 'string' && value.next_cursor ? value.next_cursor : null;
      state.fetched = true;
    } catch (error) {
      if (state === submissionHistories.get(projectId) && epoch === state.epoch) state.error = error.message;
    } finally {
      if (state === submissionHistories.get(projectId) && epoch === state.epoch) { state.loading = false; renderSubmissionProjects(); }
    }
  }
  async function versions() {
    submissionHistories.clear();
    await loadSubmissionProjects(false);
  }
  function assetMessage(value) { const node = document.querySelector('#asset-message'); if (node) text(node, value); }
  function assetPayload(submissionId, kind, name, version, alias) {
    const cleanName = String(name || '').trim(), cleanVersion = String(version || '').trim(), cleanAlias = String(alias || '').trim();
    if (!submissionId || (kind !== 'algorithm' && kind !== 'template')) throw new Error('资产类型必须是算法或模板。');
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(cleanName) || cleanName.length > 128) throw new Error('资产名称必须为最多 128 字符的字母、数字、点、下划线或连字符标识符。');
    if (!/^\d+\.\d+\.\d+$/.test(cleanVersion) || cleanVersion.length > 32) throw new Error('版本必须是最多 32 字符的 x.y.z。');
    if (kind === 'algorithm' && (!cleanAlias || /[\\/\x00]/.test(cleanAlias))) throw new Error('算法候选必须填写 project.yaml 中的 plugins 键。');
    return {submission_id: submissionId, kind, name: cleanName, version: cleanVersion, alias: kind === 'algorithm' ? cleanAlias : null};
  }
  function assetDigest(candidate) {
    if (!candidate || typeof candidate.candidate_sha256 !== 'string' || !candidate.candidate_sha256) throw new Error('候选摘要不可用，请重新读取候选。');
    return candidate.candidate_sha256;
  }
  function renderAssetCreate(submissionId) {
    const host = document.querySelector('#asset-create'); if (!host) return;
    host.replaceChildren();
    if (!submissionId) { paragraph(host, '从任一已提交固定版本（含学生版本）点击“提取资产候选”开始。'); return; }
    const panel = document.createElement('form'); panel.className = 'asset-form';
    const title = document.createElement('h2'); text(title, `从固定提交 ${submissionId} 提取候选`); panel.append(title);
    const kind = document.createElement('select');
    [['algorithm', '算法'], ['template', '模板']].forEach(item => { const option = document.createElement('option'); option.value = item[0]; text(option, item[1]); kind.append(option); });
    const kindLabel = document.createElement('label'); kindLabel.append(span('资产类型'), kind);
    const name = document.createElement('input'); name.required = true; name.maxLength = 128; name.placeholder = '单个名称，例如 rainfall.scale';
    const nameLabel = document.createElement('label'); nameLabel.append(span('名称'), name);
    const version = document.createElement('input'); version.required = true; version.pattern = '\\d+\\.\\d+\\.\\d+'; version.placeholder = '1.0.0';
    const versionLabel = document.createElement('label'); versionLabel.append(span('版本'), version);
    const alias = document.createElement('input'); alias.required = true; alias.maxLength = 128; alias.placeholder = 'project.yaml 的 plugins 键';
    const aliasLabel = document.createElement('label'); aliasLabel.append(span('算法别名'), alias);
    const updateAlias = () => { const algorithm = kind.value === 'algorithm'; alias.disabled = !algorithm; alias.required = algorithm; aliasLabel.hidden = !algorithm; };
    kind.onchange = updateAlias; updateAlias();
    const submitButton = document.createElement('button'); submitButton.type = 'submit'; text(submitButton, '提取候选（不发布）');
    panel.append(kindLabel, nameLabel, versionLabel, aliasLabel, submitButton);
    panel.onsubmit = async event => {
      event.preventDefault(); submitButton.disabled = true;
      try {
        const created = await post('/api/asset-candidates', assetPayload(submissionId, kind.value, name.value, version.value, alias.value));
        assetMessage('候选已提取为不可变内容；请审阅后再发布。');
        await assetCandidates();
        if (created?.candidate_id) { selectedAssetId = created.candidate_id; renderAssetDetail(created); }
      } catch (error) { assetMessage(error.message); }
      finally { submitButton.disabled = false; }
    };
    host.append(panel);
  }
  function assetReport(value) {
    if (typeof value === 'string') return value;
    try { return JSON.stringify(value || {}, null, 2); } catch (_) { return '静态报告不可显示。'; }
  }
  function renderAssetEvidence(content, candidate) {
    const panel = document.createElement('section'); panel.className = 'asset-evidence';
    heading(panel, '来源固定快照');
    paragraph(panel, `来源固定提交：${candidate.submission_id || '未知'} · 摘要：${candidate.source_sha256 || '未知'}`, 'path');
    paragraph(panel, '历史网页执行记录已移除。请查看固定提交中的 IDE 实验方案与产物，并进行人工审核。');
    content.append(panel);
  }
  function renderAssetDetail(candidate) {
    const section = document.querySelector('#asset-detail'), title = document.querySelector('#asset-detail-title'), content = document.querySelector('#asset-detail-content');
    if (!section || !title || !content) return;
    section.hidden = false; content.replaceChildren(); text(title, `候选 ${candidate.name || candidate.candidate_id}`);
    paragraph(content, `状态：${candidate.status || '未知'} · 类型：${candidate.kind || '未知'} · 资产：${candidate.asset_id || '未发布'}`);
    paragraph(content, `版本：${candidate.version || '未知'} · 引用：${candidate.ref || '未发布'}`);
    paragraph(content, `来源固定提交：${candidate.submission_id || '未知'} · 来源摘要：${candidate.source_sha256 || '未知'}`, 'path');
    paragraph(content, `候选摘要：${candidate.candidate_sha256 || '未知'}`, 'path');
    if (candidate.created_at) paragraph(content, `创建时间：${candidate.created_at}`);
    if (candidate.source_origin) {
      const lineage = document.createElement('pre');
      text(lineage, `来源记录（服务端）\n${assetReport(candidate.source_origin)}`); content.append(lineage);
    }
    if (candidate.alias !== null && candidate.alias !== undefined) paragraph(content, `提取别名（project.yaml plugins 键）：${candidate.alias}`);
    if (candidate.review_notes) paragraph(content, `审阅说明：${candidate.review_notes}`);
    const report = document.createElement('pre'); report.className = 'asset-report'; text(report, `静态报告\n${assetReport(candidate.static_validation)}`); content.append(report);
    const references = Array.isArray(candidate.data_references) ? candidate.data_references : [];
    paragraph(content, references.length ? '外部数据仅以引用保留，不复制进候选：' : '候选没有外部数据引用。');
    references.forEach(reference => paragraph(content, `${reference.dataset_id || '未知数据集'}/${reference.path || '未知路径'} · ${reference.sha256 || '无摘要'}`, 'path'));
    const files = Array.isArray(candidate.files) ? candidate.files : [];
    if (!files.length) paragraph(content, '候选未返回可预览文件元数据。');
    else {
      const controls = actions(content);
      files.forEach(file => {
        paragraph(content, `${file.path || '未知路径'} · ${Number.isSafeInteger(file.size) ? displayBytes(file.size) : '未知大小'} · ${file.sha256 || '无摘要'}`, 'path');
        controls.append(button(`预览 ${file.path}`, () => openAssetFile(candidate.candidate_id, file.path)));
      });
    }
    renderAssetEvidence(content, candidate);
    if (candidate.status === 'draft') renderAssetReviewForm(content, candidate);
    if (candidate.status === 'reviewed') renderAssetPublishControl(content, candidate);
    if (candidate.status === 'published') paragraph(content, '此精确版本已发布。请到“共享权限”授权；未经授权的学生不会看到它。');
  }
  function renderAssetReviewForm(content, candidate) {
    const reviewEpoch = assetDetailEpoch;
    const form = document.createElement('form'); form.className = 'asset-review-form';
    const title = document.createElement('h3'); text(title, '教师人工审阅'); form.append(title);
    const notes = document.createElement('textarea'); notes.required = true; notes.maxLength = 4000; notes.value = candidate.review_notes || ''; notes.placeholder = '说明已人工验证的内容；这不是自动单元测试或科学认证。';
    const notesLabel = document.createElement('label'); notesLabel.append(span('审阅说明（必填）'), notes);
    const explanation = document.createElement('p'); explanation.className = 'asset-note'; text(explanation, '请依据固定提交中的 IDE 实验方案、产物和人工审核记录判断；网页不会执行代码。');
    const review = document.createElement('button'); review.type = 'submit'; text(review, '保存审阅');
    form.append(notesLabel, explanation, review);
    form.onsubmit = async event => {
      event.preventDefault();
      if (!notes.value.trim()) { assetMessage('审阅说明不能为空。'); return; }
      review.disabled = true;
      try {
        const detail = await post(`/api/asset-candidates/${encodeURIComponent(candidate.candidate_id)}/review`, {expected_sha256: assetDigest(candidate), notes: notes.value.trim(), validation_run_id: null});
        if (selectedAssetId !== candidate.candidate_id || reviewEpoch !== assetDetailEpoch) return;
        assetMessage('审阅已保存。发布会使用候选固定摘要。'); renderAssetDetail(detail);
      } catch (error) { if (selectedAssetId === candidate.candidate_id && reviewEpoch === assetDetailEpoch) assetMessage(error.message); }
      finally { if (selectedAssetId === candidate.candidate_id && reviewEpoch === assetDetailEpoch) review.disabled = false; }
    };
    content.append(form);
  }
  function renderAssetPublishControl(content, candidate) {
    const panel = document.createElement('div'); panel.className = 'asset-publish';
    paragraph(panel, '审阅已保存并冻结；发布将使用当前候选固定摘要，不能覆盖同一精确版本。');
    const publish = button('发布此版本', () => publishAsset(candidate, publish, assetDetailEpoch));
    panel.append(publish); content.append(panel);
  }
  async function publishAsset(candidate, trigger, detailEpoch = assetDetailEpoch) {
    if (candidate.status !== 'reviewed') return;
    if (!confirm('发布后不能覆盖该精确版本；初始仅教师可读。确定发布吗？')) return;
    trigger.disabled = true;
    try {
      const detail = await post(`/api/asset-candidates/${encodeURIComponent(candidate.candidate_id)}/publish`, {expected_sha256: assetDigest(candidate)});
      if (selectedAssetId === candidate.candidate_id && detailEpoch === assetDetailEpoch) renderAssetDetail(detail);
      if (selectedAssetId === candidate.candidate_id && detailEpoch === assetDetailEpoch) assetMessage('已发布精确版本。请到“共享权限”授予学生访问权限。');
      await Promise.all([assetCandidates(), templates(), componentAccess()]);
    } catch (error) { if (selectedAssetId === candidate.candidate_id && detailEpoch === assetDetailEpoch) assetMessage(error.message); }
    finally { if (selectedAssetId === candidate.candidate_id && detailEpoch === assetDetailEpoch) trigger.disabled = false; }
  }
  async function openAssetCandidate(candidateId) {
    const epoch = ++assetDetailEpoch; selectedAssetId = candidateId;
    const detail = document.querySelector('#asset-detail'); if (detail) detail.hidden = true;
    try {
      const candidate = await api(`/api/asset-candidates/${encodeURIComponent(candidateId)}`);
      if (epoch !== assetDetailEpoch || selectedAssetId !== candidateId) return;
      renderAssetDetail(candidate);
    } catch (error) { if (epoch === assetDetailEpoch && selectedAssetId === candidateId) assetMessage(error.message); }
  }
  async function openAssetFile(candidateId, path) {
    const epoch = ++assetFileEpoch;
    try {
      const file = await api(`/api/asset-candidates/${encodeURIComponent(candidateId)}/file?path=${encodeURIComponent(path)}`);
      if (epoch !== assetFileEpoch || selectedAssetId !== candidateId) return;
      const name = document.querySelector('#asset-file-name'), preview = document.querySelector('#asset-file-preview');
      if (name) text(name, file.path || path); if (preview) text(preview, typeof file.text === 'string' ? file.text : '文件内容不可显示。');
    } catch (error) { if (epoch === assetFileEpoch && selectedAssetId === candidateId) assetMessage(error.message); }
  }
  function candidateOrigin(candidate) {
    const origin = candidate?.source_origin || {};
    const owner = origin.owner_username || (origin.owner_uid !== undefined && origin.owner_uid !== null ? `账号 #${origin.owner_uid}` : '来源账号不可用');
    const projectId = origin.project_id || candidate?.project_id || '来源项目不可用';
    const projectName = origin.project_name || origin.name || candidate?.source_project_name || projectId;
    return {owner, ownerKey: origin.owner_uid ?? origin.owner_username ?? 'unknown', projectId, projectName};
  }
  function renderAssetList(rows = assetListState.rows) {
    const list = document.querySelector('#asset-candidate-list'); if (!list) return;
    list.replaceChildren();
    const visible = rows.filter(candidate => {
      const origin = candidateOrigin(candidate);
      return textMatch(assetListState.query, candidate.name, candidate.candidate_id, candidate.submission_id, candidate.version, candidate.kind,
        candidate.kind === 'algorithm' ? '算法' : candidate.kind === 'template' ? '模板' : '', candidate.status, origin.owner, origin.projectName, origin.projectId);
    });
    if (!visible.length) { paragraph(list, rows.length ? '没有符合当前搜索条件的资产候选。' : '暂无资产候选。请从任一已提交固定版本（含学生版本）提取候选。'); return; }
    const owners = new Map(), allKeys = [];
    visible.forEach(candidate => {
      const origin = candidateOrigin(candidate), ownerKey = `${origin.ownerKey}\u0000${origin.owner}`;
      if (!owners.has(ownerKey)) owners.set(ownerKey, {origin, projects: new Map()});
      const projects = owners.get(ownerKey).projects;
      if (!projects.has(origin.projectId)) projects.set(origin.projectId, {origin, candidates: []});
      projects.get(origin.projectId).candidates.push(candidate);
    });
    owners.forEach((group, ownerKey) => {
      allKeys.push(`owner:${ownerKey}`);
      group.projects.forEach((project, projectId) => {
        allKeys.push(`project:${ownerKey}:${projectId}`);
        const byStatus = new Map();
        project.candidates.forEach(candidate => {
          const key = `${candidate.kind || 'unknown'}:${candidate.status || 'unknown'}`;
          if (!byStatus.has(key)) byStatus.set(key, []);
          byStatus.get(key).push(candidate);
        });
        byStatus.forEach((candidates, key) => allKeys.push(`status:${ownerKey}:${projectId}:${key}`));
      });
    });
    foldControls(list, 'candidates', allKeys, () => renderAssetList());
    owners.forEach((group, ownerKey) => {
      const ownerContent = document.createElement('div'); ownerContent.className = 'fold-content cards';
      group.projects.forEach((project, projectId) => {
        const projectContent = document.createElement('div'); projectContent.className = 'fold-content cards';
        const byStatus = new Map();
        project.candidates.forEach(candidate => {
          const key = `${candidate.kind || 'unknown'}:${candidate.status || 'unknown'}`;
          if (!byStatus.has(key)) byStatus.set(key, []);
          byStatus.get(key).push(candidate);
        });
        byStatus.forEach((candidates, key) => {
          const content = document.createElement('div'); content.className = 'fold-content cards';
          candidates.forEach(candidate => {
            const row = card(); heading(row, `${candidate.kind === 'template' ? '模板' : '算法'} · ${candidate.name || candidate.candidate_id} · ${candidate.version || '未知版本'}`);
            paragraph(row, `状态：${candidate.status || '未知'} · 来源提交：${candidate.submission_id || '未知'}`);
            paragraph(row, `候选摘要：${candidate.candidate_sha256 || '未知'}`, 'path');
            actions(row).append(button('打开候选', () => openAssetCandidate(candidate.candidate_id)));
            content.append(row);
          });
          const [kind, status] = key.split(':');
          projectContent.append(foldDetails('candidates', `status:${ownerKey}:${projectId}:${key}`, `${kind === 'template' ? '模板' : kind === 'algorithm' ? '算法' : '类型未知'} · ${status || '状态未知'} · ${candidates.length} 个候选`, content, 'candidate-status-group'));
        });
        ownerContent.append(foldDetails('candidates', `project:${ownerKey}:${projectId}`, `来源项目：${project.origin.projectName} · ${projectId} · ${project.candidates.length} 个候选`, projectContent, 'candidate-project-group'));
      });
      list.append(foldDetails('candidates', `owner:${ownerKey}`, `来源账号：${group.origin.owner} · 当前显示 ${[...group.projects.values()].reduce((count, project) => count + project.candidates.length, 0)} 个候选`, ownerContent, 'candidate-owner-group'));
    });
  }
  async function assetCandidates() {
    if (me?.role !== 'teacher') return;
    const epoch = ++assetListEpoch;
    try {
      const rows = await api('/api/asset-candidates');
      if (epoch !== assetListEpoch) return;
      assetListState.rows = Array.isArray(rows) ? rows : [];
      renderAssetList();
    } catch (error) { if (epoch === assetListEpoch) assetMessage(error.message); }
  }
  function openAssetCandidateForm(submissionId) {
    if (me?.role !== 'teacher') return;
    showPage('asset-candidates'); renderAssetCreate(submissionId); assetCandidates();
  }
  async function distributions() {
    const epoch = ++distributionEpoch;
    const rows = await api('/api/distributions');
    if (epoch !== distributionEpoch) return;
    distributionState.rows = Array.isArray(rows) ? rows : [];
    renderDistributions();
  }
  function renderDistributionCard(value, states) {
    const row = card(); heading(row, `${value.source_project_name} · 基线 ${value.source_version}`);
    paragraph(row, `目标账号：${value.target_username} · ${states[value.status] || value.status}`);
    paragraph(row, value.distribution_id, 'path');
    if (value.target_uid === me?.uid && value.target_username === me?.username) {
      const claim = button(value.status === 'claimed' ? '打开已领取项目' : '领取到我的工作区', async () => {
        claim.disabled = true;
        try {
          const project = await post(`/api/distributions/${encodeURIComponent(value.distribution_id)}/claim`, {});
          await Promise.all([projects(), distributions()]); showPage('projects');
          text(output, `项目已就绪：${project.path}`);
        } catch (error) { text(output, error.message); }
        finally { claim.disabled = false; }
      });
      actions(row).append(claim);
    }
    return row;
  }
  function renderDistributions() {
    const list = document.querySelector('#distribution-list'); if (!list) return; list.replaceChildren();
    text(document.querySelector('#distribution-intro'), me?.role === 'teacher'
      ? '这里是你的个人领取区，与学生使用相同流程。完整任务可直接领取为新项目；只有算法和数据的资源包也可加入已有项目。'
      : '教师分发的资源自动可用。含案例任务可开始为个人项目，资源包可按需加入已有项目。');
    const states = {pending: '待领取', claiming: '领取待完成，可重试', claimed: '已领取'};
    const taskDistributions = new Set(taskState.personal.items.map(task => task.distribution_id).filter(Boolean));
    const visible = distributionState.rows.filter(value => !taskDistributions.has(value.distribution_id) && textMatch(distributionState.query, value.target_username, value.target_uid, value.source_project_name, value.source_version, value.distribution_id, states[value.status] || value.status));
    if (!visible.length) { if (!taskState.personal.items.length) paragraph(list, distributionState.rows.length ? '没有符合当前搜索条件的分发记录。' : '暂无旧版分发记录。'); return; }
    paragraph(list, '旧版独立分发记录（新任务在上方任务卡片统一领取）', 'asset-note');
    if (me?.role === 'teacher') {
      const targets = new Map(), allKeys = [];
      visible.forEach(value => {
        const targetKey = `${value.target_uid ?? 'unknown'}\u0000${value.target_username || '账号不可用'}`;
        if (!targets.has(targetKey)) targets.set(targetKey, []);
        targets.get(targetKey).push(value);
      });
      targets.forEach((values, targetKey) => {
        allKeys.push(`target:${targetKey}`);
        new Set(values.map(value => value.status || 'unknown')).forEach(status => allKeys.push(`status:${targetKey}:${status}`));
      });
      foldControls(list, 'distributions', allKeys, renderDistributions);
      targets.forEach((values, targetKey) => {
        const content = document.createElement('div'); content.className = 'fold-content cards';
        const statuses = new Map(); values.forEach(value => { const status = value.status || 'unknown'; if (!statuses.has(status)) statuses.set(status, []); statuses.get(status).push(value); });
        statuses.forEach((statusRows, status) => {
          const statusContent = document.createElement('div'); statusContent.className = 'fold-content cards';
          statusRows.forEach(value => statusContent.append(renderDistributionCard(value, states)));
          content.append(foldDetails('distributions', `status:${targetKey}:${status}`, `${states[status] || status} · ${statusRows.length} 条分发`, statusContent, 'distribution-status-group'));
        });
        const first = values[0];
        list.append(foldDetails('distributions', `target:${targetKey}`, `目标账号：${first.target_username || '账号不可用'} · 当前显示 ${values.length} 条分发`, content, 'distribution-target-group'));
      });
    } else {
      const groups = new Map([['pending', []], ['claimed', []]]);
      visible.forEach(value => groups.get(value.status === 'claimed' ? 'claimed' : 'pending').push(value));
      const keys = [...groups.entries()].filter(([, values]) => values.length).map(([status]) => `student:${status}`);
      foldControls(list, 'distributions', keys, renderDistributions);
      groups.forEach((values, status) => {
        if (!values.length) return;
        const content = document.createElement('div'); content.className = 'fold-content cards';
        values.forEach(value => content.append(renderDistributionCard(value, states)));
        const label = status === 'claimed' ? '已领取' : '待领取或领取待完成';
        list.append(foldDetails('distributions', `student:${status}`, `${label} · 当前显示 ${values.length} 条分发`, content, 'distribution-student-group'));
      });
    }
  }
  const workflowRequestId = () => globalThis.crypto?.randomUUID?.() || 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => { const r = Math.floor(Math.random() * 16); return (c === 'x' ? r : (r & 3) | 8).toString(16); });
  // Keep the same id for a retry of the exact form.  The broker uses that id
  // plus its payload fingerprint to make a dropped response safe to resend.
  function idempotentRequest(state, payload) {
    const fingerprint = JSON.stringify(payload);
    if (state.fingerprint !== fingerprint) { state.fingerprint = fingerprint; state.requestId = workflowRequestId(); }
    return {request_id: state.requestId, ...payload};
  }
  function renderPreparation(preparation, host) {
    host.replaceChildren();
    heading(host, `固定提交 · ${preparation.status === 'unchanged' ? '内容未变化' : '已保存'}`);
    if (preparation.project_id || preparation.source_project_id) paragraph(host, `项目：${preparation.project_id || preparation.source_project_id}`, 'path');
    if (preparation.content_sha256) paragraph(host, `固定快照摘要：${preparation.content_sha256}`, 'path');
    if (preparation.workspace_warning) paragraph(host, preparation.workspace_warning, 'action-result error');
    if (preparation.reason) paragraph(host, preparation.reason);
    if (preparation.submission_id || preparation.final_submission_id) paragraph(host, `已保存固定提交：${preparation.submission_id || preparation.final_submission_id}`, 'path');
  }
  function renderSubmissionConfirmation(project, kind, preview, resultPaths = []) {
    output.replaceChildren();
    const panel = card(); heading(panel, `${kind === 'major' ? '大' : '小'}版本固定提交`);
    paragraph(panel, '确认后立即保存 IDE 当前代码、方案和所选产物的不可变副本。网页不会检查或运行代码。');
    const form = document.createElement('form'); form.className = 'asset-review-form';
    const caseRow = preview.case || {}, caseLabel = document.createElement('label'), caseInput = document.createElement('input');
    caseInput.required = true; caseInput.value = caseRow.suggested_version || caseRow.current_version || '';
    caseLabel.append(span(`案例版本${caseRow.changed ? '（内容已变化）' : '（内容未变化）'}`), caseInput); form.append(caseLabel);
    const algorithmVersions = {};
    const selectedResults = new Set(resultPaths);
    const retry = {fingerprint: null, requestId: null};
    (preview.algorithms || []).forEach(item => {
      const label = document.createElement('label'), input = document.createElement('input'); input.required = true;
      input.value = item.suggested_version || item.current_version || item.previous_version || '';
      label.append(span(`${item.alias || item.name || '算法'} 版本${item.changed ? '（内容已变化）' : '（内容未变化）'}`), input); form.append(label);
      algorithmVersions[item.alias] = input;
    });
    const confirmButton = button('确认固定提交', async () => {
      confirmButton.disabled = true;
      try {
        const selectedVersions = {}; Object.entries(algorithmVersions).forEach(([alias, input]) => { selectedVersions[alias] = input.value.trim(); });
        const prepared = await post(`/api/projects/${encodeURIComponent(project)}/submission-prepare`, idempotentRequest(retry, {
          kind, expected_revision: preview.snapshot_revision, case_version: caseInput.value.trim(), algorithm_versions: selectedVersions,
          result_paths: [...selectedResults].sort(),
        }));
        renderPreparation(prepared, panel);
        if (prepared.status === 'succeeded' || prepared.status === 'unchanged') {
          try { await versions(); } catch (_) { paragraph(panel, '固定提交已保存；版本列表暂未刷新，可稍后刷新查看。', 'action-result success'); }
        }
      } catch (error) { paragraph(panel, error.message); confirmButton.disabled = false; }
    });
    confirmButton.type = 'button'; form.append(confirmButton); panel.append(form); output.append(panel);
  }
  async function submit(project, kind) {
    try {
      if (dirty) { text(output, '编辑器有未保存内容；请先保存或重新加载后再提交。'); return; }
      const info = await api(`/api/projects/${encodeURIComponent(project)}/experiments`), selected = new Set();
      const panel = card(); heading(panel, '选择要固定的 IDE 产物');
      paragraph(panel, '代码和方案会自动固定。默认勾选每个方案最新成功批次的小产物（单份不超过 2 MiB，总计不超过 16 MiB）；也可选择其他批次或旧产物。未勾选文件仍在个人项目，可另存为派生数据。');
      const latest = new Map();
      [...(info.runs || [])].filter(run => ['succeeded', 'success'].includes(String(run.record?.status || run.record?.state || '').toLowerCase()))
        .sort((a, b) => String(b.record?.started_at || b.batch_id).localeCompare(String(a.record?.started_at || a.batch_id)))
        .forEach(run => { if (!latest.has(run.scheme_id)) latest.set(run.scheme_id, run); });
      let defaultBytes = 0;
      latest.forEach(run => (run.files || []).forEach(file => {
        const size = Number(file.size || 0);
        if (size <= 2 * 1024 * 1024 && defaultBytes + size <= 16 * 1024 * 1024 && selected.size < 200) { selected.add(file.path); defaultBytes += size; }
      }));
      const allFiles = info.result_files || (info.runs || []).flatMap(run => run.files || []);
      const selectionNote = document.createElement('p'); panel.append(selectionNote);
      const updateSelection = () => {
        const included = allFiles.filter(file => selected.has(file.path)), size = included.reduce((total, file) => total + Number(file.size || 0), 0);
        text(selectionNote, `将保存 ${included.length} 个产物，${displayBytes(size)}；不保存 ${allFiles.length - included.length} 个。代码与方案另行自动包含，服务器会核对总量上限。`);
      };
      allFiles.forEach(file => {
        const row = document.createElement('label'), input = document.createElement('input'); input.type = 'checkbox'; input.checked = selected.has(file.path);
        input.onchange = () => { if (input.checked) selected.add(file.path); else selected.delete(file.path); updateSelection(); };
        row.append(input, span(`${file.path} · ${displayBytes(file.size || 0)}`)); panel.append(row);
      });
      updateSelection();
      if (!allFiles.length) paragraph(panel, '暂无产物；仍可固定提交代码和方案。');
      const next = button('确认产物选择并预览版本', async () => {
        next.disabled = true;
        try { const resultPaths = [...selected].sort(), preview = await post(`/api/projects/${encodeURIComponent(project)}/submission-preview`, {kind, result_paths: resultPaths}); renderSubmissionConfirmation(project, kind, preview, resultPaths); }
        catch (error) { paragraph(panel, error.message, 'action-result error'); next.disabled = false; }
      }); panel.append(next); output.replaceChildren(panel);
    } catch (error) { text(output, error.message); }
  }
  document.querySelector('#create').onsubmit = async event => {
    event.preventDefault();
    const submit = event.target.querySelector('button'); submit.disabled = true;
    try { const form = new FormData(event.target), payload = {name: form.get('name')}, templateRef = form.get('template_ref'); if (templateRef) payload.template_ref = templateRef; await post('/api/projects', payload); event.target.reset(); await projects(); }
    catch (error) { text(output, error.message); }
    finally { submit.disabled = false; }
  };
  document.querySelector('#import').onsubmit = async event => {
    event.preventDefault();
    try { const form = new FormData(event.target); await post('/api/projects/import', {name: form.get('name'), path: form.get('path')}); event.target.reset(); await projects(); }
    catch (error) { text(output, error.message); }
  };
  async function importPackage(formNode, submit) {
    const form = new FormData(formNode), file = form.get('package');
    if (!(file instanceof File) || !file.size) { text(output, '请选择非空 ZIP 包。'); return; }
    if (file.size > 68 * 1024 * 1024) { text(output, 'ZIP 包超过 68 MiB 上限。'); return; }
    submit.disabled = true;
    try {
      const name = String(form.get('name') || '').trim(), res = await fetch(`/api/imports?name=${encodeURIComponent(name)}`, {method: 'POST', headers: {'Authorization': `Bearer ${bearer || ''}`, 'Content-Type': 'application/zip'}, body: file, credentials: 'omit'});
      let result = {}; try { result = await res.json(); } catch (_) {}
      if (!res.ok) { const error = new Error(result.error || `HTTP ${res.status}`); error.status = res.status; throw error; }
      formNode.reset(); await projects(); text(output, `${sourceLabel(result)}；项目已准备好，已有目录会保留修改。`);
    } catch (error) { text(output, error.message); }
    finally { submit.disabled = false; }
  }
  document.querySelector('#package-import').onsubmit = async event => {
    event.preventDefault();
    await importPackage(event.target, event.target.querySelector('button'));
  };
  document.querySelector('#editor').oninput = () => { dirty = true; text(document.querySelector('#editor-state'), '有未保存编辑；提交只会使用磁盘内容。'); };
  document.querySelector('#save').onclick = async () => {
    if (!current) return;
    try {
      const result = await api(`/api/projects/${encodeURIComponent(current.project_id)}/file`, {method: 'PUT', body: JSON.stringify({path: current.path, text: document.querySelector('#editor').value, expected_sha256: current.sha256})});
      current.sha256 = result.sha256; dirty = false; text(document.querySelector('#editor-state'), '已保存到与 IDE 共用的文件。');
    } catch (error) { text(output, error.message + ' 请重新打开文件后再保存。'); }
  };
  document.querySelector('#compare-left').onclick = () => { left = current; document.querySelector('#compare').disabled = !(left && right); updateCompareStatus(); text(output, '已选择比较左侧版本。'); };
  document.querySelector('#compare-right').onclick = () => { right = current; document.querySelector('#compare').disabled = !(left && right); updateCompareStatus(); text(output, '已选择比较右侧版本。'); };
  document.querySelector('#compare').onclick = async () => {
    try {
      if (!left || !right || left.path !== right.path) throw new Error('请选择同一路径文件的两个固定版本。');
      const value = await api(`/api/diff?before=${encodeURIComponent(left.submission_id)}&after=${encodeURIComponent(right.submission_id)}&path=${encodeURIComponent(left.path)}`);
      text(output, value.diff || '文件内容相同。');
    } catch (error) { text(output, error.message); }
  };
  document.querySelector('#baseline-compare').onclick = async () => {
    try {
      const value = await api(`/api/baseline-diff?submission_id=${encodeURIComponent(current.submission_id)}&path=${encodeURIComponent(current.path)}`);
      text(output, value.diff || '与教师固定基线相同。');
    } catch (error) { text(output, error.message); }
  };
  function targetNames(value) {
    return [...new Set(value.split(',').map(name => name.trim()).filter(Boolean))];
  }
  function dataReference(file) {
    const datasetId = file?.dataset_id, path = file?.path, size = file?.size, sha256 = file?.sha256;
    if (typeof datasetId !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(datasetId) || datasetId.length > 128) throw new Error('数据集 ID 无效。');
    if (typeof path !== 'string' || !path || path.includes('\\') || path.startsWith('/') || path.split('/').some((part, index) => !part || part === '.' || part === '..' || (index === 0 && part.includes(':')))) throw new Error('数据集路径无效。');
    if (!Number.isSafeInteger(size) || size < 0) throw new Error('数据集文件大小无效。');
    if (typeof sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(sha256)) throw new Error('数据集文件摘要无效。');
    return {dataset_id: datasetId, path, size, sha256};
  }
  function dataReferenceKey(file) { return `${file.dataset_id}\u0000${file.path}`; }
  function dataReferences(files) {
    const references = files.map(dataReference);
    if (references.length > DATA_REFERENCE_LIMITS.count) throw new Error(`数据引用超过 ${DATA_REFERENCE_LIMITS.count} 个上限。`);
    let total = 0;
    references.forEach(reference => {
      if (reference.size > DATA_REFERENCE_LIMITS.fileBytes) throw new Error(`文件 ${reference.path} 超过单个 512 MiB 上限。`);
      total += reference.size;
      if (total > DATA_REFERENCE_LIMITS.totalBytes) throw new Error('数据引用合计超过 2 GiB 上限。');
    });
    return references;
  }
  function displayBytes(value) {
    if (value >= 1024 * 1024 * 1024) return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
    if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(2)} MiB`;
    if (value >= 1024) return `${(value / 1024).toFixed(2)} KiB`;
    return `${value} B`;
  }
  function runnerDatasetPath(reference) { return `/datasets/${reference.dataset_id}/${reference.path}`; }
  function updateDatasetSelection() {
    const summary = document.querySelector('#dataset-summary'), download = document.querySelector('#download-data-references');
    try {
      const references = dataReferences([...selectedDatasets.values()]);
      const total = references.reduce((value, reference) => value + reference.size, 0);
      text(summary, references.length ? `已选 ${references.length} 个引用，合计 ${displayBytes(total)}。` : '尚未选择数据文件。');
      download.disabled = !references.length;
      return references;
    } catch (error) {
      text(summary, error.message); download.disabled = true; return null;
    }
  }
  function downloadDataReferences() {
    const references = updateDatasetSelection();
    if (!references?.length) return;
    const blob = new Blob([JSON.stringify(references, null, 2) + '\n'], {type: 'application/json'});
    const url = URL.createObjectURL(blob), link = document.createElement('a');
    link.href = url; link.download = 'DATA-REFERENCES.json'; link.hidden = true; document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }
  function catalogDatasetFiles(dataset) {
    return Array.isArray(dataset?.files) ? dataset.files.map(file => {
      try { return dataReference({...file, dataset_id: file?.dataset_id || dataset.dataset_id}); }
      catch (_) { return null; }
    }).filter(Boolean) : [];
  }
  function datasetDisplaySize(dataset, files) {
    const total = Number.isSafeInteger(dataset?.total_size) && dataset.total_size >= 0
      ? dataset.total_size : files.reduce((value, file) => value + file.size, 0);
    return displayBytes(total);
  }
  function projectDatasetSummary(references) {
    const groups = Array.isArray(references?.datasets) ? references.datasets : [];
    if (!groups.length) return '该项目暂未引用受控数据。';
    return `项目已引用 ${groups.length} 个数据集，共 ${groups.reduce((total, group) => total + Number(group.file_count || 0), 0)} 个标准引用。`;
  }
  function updateProjectDatasetSelection() {
    const summary = document.querySelector('#dataset-attach-summary'), attach = document.querySelector('#add-project-datasets');
    if (!summary || !attach) return;
    const selected = [...selectedProjectDatasets];
    if (!datasetAttachState.projectId) text(summary, '先选择当前账号的项目，再勾选数据集。');
    else if (!selected.length) text(summary, '勾选下方一个或多个完整数据集后加入项目。');
    else text(summary, `将 ${selected.length} 个完整数据集加入当前项目；已有相同引用不会重复写入。`);
    attach.disabled = !datasetAttachState.projectId || !selected.length || !datasetAttachState.references;
  }
  function renderDatasetProjectSelector() {
    const attach = document.querySelector('#dataset-project-attach'), select = document.querySelector('#dataset-project');
    if (!attach || !select) return;
    attach.hidden = !datasetAttachState.projects.length;
    const initial = datasetAttachState.projectId;
    const placeholder = document.createElement('option'); placeholder.value = ''; text(placeholder, '选择当前账号的项目');
    select.replaceChildren(placeholder);
    datasetAttachState.projects.forEach(project => {
      if (!project?.project_id) return;
      const option = document.createElement('option'); option.value = project.project_id;
      text(option, `${project.archived ? '已归档 · ' : ''}${project.name || project.project_id} · ${project.project_id}`);
      select.append(option);
    });
    if (datasetAttachState.projects.some(project => project?.project_id === initial)) select.value = initial;
    else datasetAttachState.projectId = '';
    renderDerivedProjectFiles();
    updateProjectDatasetSelection();
  }
  function renderDerivedProjectFiles() {
    const select = document.querySelector('#derived-project-file'), name = document.querySelector('#derived-project-name');
    const save = document.querySelector('#save-project-derived');
    if (!select || !save) return;
    const project = datasetAttachState.projects.find(value => value?.project_id === datasetAttachState.projectId);
    const placeholder = document.createElement('option'); placeholder.value = ''; text(placeholder, project ? '选择项目中的普通文件' : '先选择项目');
    select.replaceChildren(placeholder);
    if (project && !project.unavailable) {
      const derivable = path => ['data/', 'artifacts/', 'results/', 'report/'].some(prefix => path.startsWith(prefix));
      (project.files || []).filter(file => typeof file?.path === 'string' && derivable(file.path)
        && file.reference_only !== true && file.managed_reference !== true && file.directory !== true).forEach(file => {
        const option = document.createElement('option'); option.value = file.path;
        text(option, `${file.path}${Number.isSafeInteger(file.size) ? ` · ${displayBytes(file.size)}` : ''}`); select.append(option);
      });
    }
    if ([...select.children].some(option => option.value === datasetAttachState.sourcePath)) select.value = datasetAttachState.sourcePath;
    else datasetAttachState.sourcePath = '';
    save.disabled = !datasetAttachState.projectId || !datasetAttachState.sourcePath || !name?.value.trim();
  }
  function updateDerivedProjectUpload() {
    const select = document.querySelector('#derived-project-file'), name = document.querySelector('#derived-project-name');
    const save = document.querySelector('#save-project-derived');
    if (!select || !save) return;
    datasetAttachState.sourcePath = select.value;
    save.disabled = !datasetAttachState.projectId || !select.value || !name?.value.trim();
  }
  async function saveProjectDerived() {
    const projectId = datasetAttachState.projectId;
    const select = document.querySelector('#derived-project-file'), name = document.querySelector('#derived-project-name');
    const trigger = document.querySelector('#save-project-derived');
    if (!projectId || !select?.value || !name?.value.trim()) return;
    if (trigger) trigger.disabled = true;
    try {
      const descEl = document.querySelector('#derived-project-description');
      const tagsEl = document.querySelector('#derived-project-tags');
      const parentEl = document.querySelector('#derived-project-parent');
      const readmeEl = document.querySelector('#derived-project-readme');
      const derivedPayload = {path: select.value, name: name.value.trim()};
      if (descEl?.value?.trim()) derivedPayload.description = descEl.value.trim();
      if (tagsEl?.value?.trim()) derivedPayload.tags = tagsEl.value.split(/[,，\n]+/).map(s => s.trim()).filter(Boolean);
      if (parentEl?.value) derivedPayload.parent_dataset_id = parentEl.value;
      if (readmeEl?.value?.trim()) derivedPayload.readme_markdown = readmeEl.value.trim();
      const record = await post(`/api/projects/${encodeURIComponent(projectId)}/derived`, derivedPayload);
      text(output, `已保存派生数据：${record.name || name.value.trim()}。可将它加入项目并使用固定版本引用；${record.source_reference_only_after_attach ? '本地原文件保留，加入引用后提交不会再打包这份副本。' : '原文件保留在项目中；大型产物建议放在 data/processed/ 或 artifacts/ 后登记。'}`);
      if (datasetAttachState.projectId !== projectId) return;
      name.value = ''; datasetAttachState.sourcePath = '';
      if (descEl) descEl.value = '';
      if (tagsEl) tagsEl.value = '';
      if (readmeEl) readmeEl.value = '';
      await datasets();
    } catch (error) { text(output, error.message); }
    finally { updateDerivedProjectUpload(); }
  }
  function renderProjectDatasetReferencesList(projectId, value) {
    const list = document.querySelector('#project-dataset-references-list');
    if (!list) return;
    list.replaceChildren();
    const groups = Array.isArray(value?.datasets) ? value.datasets : [];
    groups.forEach(group => {
      const did = group.dataset_id || '';
      if (!did) return;
      if (group.stale || group.exists === false) {
        const banner = document.createElement('div'); banner.className = 'stale-dataset-badge';
        banner.append(span(`${group.name || did} (${did})`));
        const rmBtn = button('[该数据集已被管理员清理 · 点击移除引用]', async () => {
          rmBtn.disabled = true;
          try {
            await post(`/api/projects/${encodeURIComponent(projectId)}/stale-datasets/remove`, {dataset_id: did, expected_sha256: value?.sha256 ?? null});
            text(output, `已从项目移除失效数据集引用：${did}`);
            await loadProjectDatasetReferences(projectId);
          } catch (error) { text(output, error.message); rmBtn.disabled = false; }
        });
        rmBtn.className = 'stale-remove-btn';
        banner.append(rmBtn);
        list.append(banner);
      } else {
        paragraph(list, `${group.name || did} · ${did} · ${group.file_count || 0} 个文件`, 'path');
      }
    });
  }
  async function loadProjectDatasetReferences(projectId) {
    const state = document.querySelector('#dataset-project-state');
    const epoch = ++datasetReferenceEpoch;
    datasetAttachState.references = null;
    const refList = document.querySelector('#project-dataset-references-list'); if (refList) refList.replaceChildren();
    renderDerivedProjectFiles();
    updateProjectDatasetSelection();
    if (!projectId) { if (state) text(state, '选择项目后查看它已有的数据集引用。'); return; }
    if (state) text(state, '正在读取项目的数据集引用…');
    try {
      const value = await api(`/api/projects/${encodeURIComponent(projectId)}/datasets`);
      if (epoch !== datasetReferenceEpoch || datasetAttachState.projectId !== projectId) return;
      datasetAttachState.references = value;
      if (state) text(state, projectDatasetSummary(value));
      renderProjectDatasetReferencesList(projectId, value);
    } catch (error) {
      if (epoch !== datasetReferenceEpoch || datasetAttachState.projectId !== projectId) return;
      if (state) text(state, error.message); text(output, error.message);
    }
    updateProjectDatasetSelection();
  }
  async function attachProjectDatasets() {
    const projectId = datasetAttachState.projectId, trigger = document.querySelector('#add-project-datasets');
    const expected = datasetAttachState.references?.sha256;
    if (!projectId || !Array.isArray([...selectedProjectDatasets]) || !datasetAttachState.references) return;
    if (trigger) trigger.disabled = true;
    try {
      const value = await api(`/api/projects/${encodeURIComponent(projectId)}/datasets`, {
        method: 'PUT', body: JSON.stringify({dataset_ids: [...selectedProjectDatasets], expected_sha256: expected ?? null}),
      });
      if (datasetAttachState.projectId !== projectId) return;
      datasetAttachState.references = value;
      const state = document.querySelector('#dataset-project-state');
      if (state) text(state, projectDatasetSummary(value));
      text(output, `已将 ${selectedProjectDatasets.size} 个数据集的标准引用加入项目；请保存其他修改后提交固定版本。`);
    } catch (error) { if (datasetAttachState.projectId === projectId) { text(output, error.message); await loadProjectDatasetReferences(projectId); } }
    finally { updateProjectDatasetSelection(); }
  }
  function datasetFileBrowser(datasetId) {
    const details = document.createElement('details'), summary = document.createElement('summary');
    text(summary, '浏览文件路径（无需逐文件加入）'); details.append(summary);
    const query = document.createElement('input'); query.type = 'search'; query.placeholder = '筛选数据集内的文件路径';
    const listing = document.createElement('div'), status = document.createElement('p');
    let epoch = 0;
    const load = async (offset = 0) => {
      const requestEpoch = ++epoch;
      text(status, '正在读取文件清单…');
      try {
        const result = await api(`/api/datasets/${encodeURIComponent(datasetId)}/files?q=${encodeURIComponent(query.value)}&offset=${offset}&limit=100`);
        if (requestEpoch !== epoch) return;
        if (!offset) listing.replaceChildren();
        (result.items || []).forEach(file => paragraph(listing, `${file.path} · ${displayBytes(file.size)} · ${file.sha256}`, 'path'));
        text(status, `共 ${result.total} 个匹配文件。读取代码使用这里的相对路径；项目引用仍然按整个数据集添加。`);
        const oldMore = details.querySelector('.dataset-files-more'); if (oldMore) oldMore.remove();
        if (Number.isSafeInteger(result.next_offset)) { const more = button('加载更多文件路径', () => load(result.next_offset)); more.className = 'dataset-files-more'; details.append(more); }
      } catch (error) { if (requestEpoch === epoch) text(status, error.message); }
    };
    const search = button('查找文件路径', () => load());
    details.append(query, search, status, listing);
    let loaded = false;
    details.ontoggle = () => { if (details.open && !loaded) { loaded = true; load(); } };
    return details;
  }
  function renderDatasets(rows, preserveSelection = false) {
    const list = document.querySelector('#dataset-list'), selection = document.querySelector('#dataset-selection'), message = document.querySelector('#datasets-message');
    datasetAttachState.rows = rows; list.replaceChildren();
    if (!preserveSelection) { selectedDatasets.clear(); selectedProjectDatasets.clear(); }
    const personalRows = personalDatasetVisibility.showHidden ? rows : rows.filter(dataset => !datasetHidden(dataset?.dataset_id));
    if (selection) selection.hidden = !personalRows.length;
    const parentSelect = document.querySelector('#derived-project-parent');
    if (parentSelect) {
      const prev = parentSelect.value;
      parentSelect.replaceChildren();
      const blank = document.createElement('option'); blank.value = ''; text(blank, '无父级 / 全新衍生'); parentSelect.append(blank);
      personalRows.forEach(ds => {
        if (!ds?.dataset_id) return;
        const opt = document.createElement('option'); opt.value = ds.dataset_id;
        text(opt, `${ds.name || ds.dataset_id} · ${ds.dataset_id}`);
        parentSelect.append(opt);
      });
      parentSelect.value = prev;
    }
    renderDatasetProjectSelector();
    const query = document.querySelector('#dataset-query')?.value || '';
    const visible = personalRows.filter(dataset => textMatch(query, dataset.dataset_id, dataset.name, dataset.description, dataset.wiki_ref, ...(Array.isArray(dataset.tags) ? dataset.tags : [])));
    const exportEntry = dataset => ({id: dataset.dataset_id, name: dataset.name, tags: dataset.tags,
      hasReadme: String(dataset.provenance_ref || '').startsWith('README:'),
      load: () => api(`/api/datasets/${encodeURIComponent(dataset.dataset_id)}/readme`)});
    const exportAll = document.querySelector('#dataset-export');
    if (exportAll && window.ResearchKitReadmeExport) window.ResearchKitReadmeExport.bind(exportAll,
      document.querySelector('#dataset-export-status'), async () => visible.map(exportEntry), {combined: true});
    renderPersonalDatasetVisibilityControl();
    if (!personalRows.length) {
      text(message, rows.length ? '已移除的数据已隐藏；勾选“显示已移除数据”可恢复显示。' : '没有可用受控数据。请联系管理员登记数据并授权给你的服务器账号。');
      updateDatasetSelection(); return;
    }
    text(message, '选择完整数据集加入当前项目；只保存数据引用，不复制原始数据。逐文件下载入口保留在页面底部。');
    foldControls(list, 'datasets', visible.map(dataset => `dataset:${dataset.dataset_id}`), () => renderDatasets(rows, true));
    if (!visible.length) paragraph(list, '没有符合搜索条件的数据集。');
    const versionGroups = new Map();
    visible.forEach(dataset => {
      const row = card(), datasetId = typeof dataset.dataset_id === 'string' ? dataset.dataset_id : '未知数据集';
      heading(row, dataset.name ? `${dataset.name} · ${datasetId}` : datasetId);
      const visibility = String(dataset.visibility || 'public').toLowerCase();
      const visibilityLabel = {
        private: '仅本人可用',
        review: '教师审核可读（未入平台公共库）',
        public: '平台可复用',
      }[visibility] || visibility;
      paragraph(row, `所有者：${dataset.owner_username || '平台'} · 可见性：${visibilityLabel}`, 'asset-note');
      if (Array.isArray(dataset.tags) && dataset.tags.length) {
        const tags = document.createElement('div'); tags.className = 'dataset-tags';
        dataset.tags.forEach(value => { const label = span(value); label.className = 'dataset-tag'; tags.append(label); }); row.append(tags);
      }
      if (String(dataset.provenance_ref || '').startsWith('README:')) {
        heading(row, 'README 说明');
        const preview = document.createElement('div'); markdown(preview, String(dataset.description || '').slice(0, 240), 'dataset-readme'); row.append(preview);
        const details = document.createElement('details'), summary = document.createElement('summary'), body = document.createElement('div');
        details.className = 'readme-expand'; text(summary, '展开完整说明'); details.append(summary, body); row.append(details);
        let loaded = false, loading = false;
        const load = async () => {
          if (loading || loaded) return;
          loading = true; body.replaceChildren(); paragraph(body, '正在读取完整 README…');
          try {
            const info = await api(`/api/datasets/${encodeURIComponent(datasetId)}/readme`); body.replaceChildren();
            if (window.ResearchKitMarkdown?.appendReadme) window.ResearchKitMarkdown.appendReadme(body, info);
            else { markdown(body, info.markdown, 'dataset-readme'); if (info.complete === false) paragraph(body, info.notice || '请由教师刷新扫描后重试。'); }
            loaded = info.complete !== false;
            if (!loaded) body.append(button('重试读取完整说明', load));
          } catch (error) { body.replaceChildren(); paragraph(body, error.message); body.append(button('重试读取完整说明', load)); }
          finally { loading = false; }
        };
        details.ontoggle = async () => { text(summary, details.open ? '收起完整说明' : '展开完整说明'); preview.hidden = details.open; if (details.open) await load(); };
      }
      else if (dataset.description) paragraph(row, String(dataset.description));
      if (window.ResearchKitReadmeExport) {
        const exportOne = button('导出 README.md'), status = document.createElement('p'); status.setAttribute?.('aria-live', 'polite');
        exportOne.disabled = !exportEntry(dataset).hasReadme;
        if (exportOne.disabled) exportOne.title = '此数据集未提供 README.md';
        window.ResearchKitReadmeExport.bind(exportOne, status, async () => [exportEntry(dataset)]);
        actions(row).append(exportOne); row.append(status);
      }
      if (dataset.wiki_ref) paragraph(row, `知识库索引：${String(dataset.wiki_ref)} · 说明状态：${dataset.wiki_fact_state || '未确认'}`, 'path');
      if (dataset.usage) paragraph(row, `用途：${dataset.usage}`);
      if (dataset.format) paragraph(row, `格式：${dataset.format}`);
      if (dataset.student_conditions) paragraph(row, `学生使用条件：${dataset.student_conditions}`);
      if (dataset.license) paragraph(row, `使用条件：${dataset.license}`);
      if (dataset.provenance_ref) paragraph(row, `说明出处：${dataset.provenance_ref}`, 'path');
      const choose = taskProjectSelect({project_id: datasetAttachState.projectId});
      const useMessage = document.createElement('p');
      const hidden = datasetHidden(datasetId);
      const add = button('加入项目', async () => {
        const projectId = choose.value;
        if (!projectId) { text(useMessage, '请选择自己的目标项目。'); return; }
        add.disabled = true;
        try {
          const current = await api(`/api/projects/${encodeURIComponent(projectId)}/datasets`);
          await api(`/api/projects/${encodeURIComponent(projectId)}/datasets`, {method:'PUT', body:JSON.stringify({dataset_ids:[datasetId], expected_sha256:current.sha256 ?? null})});
          useMessage.className = 'action-result success'; text(useMessage, '数据材料已处理。');
          if (datasetAttachState.projectId === projectId) await loadProjectDatasetReferences(projectId);
        } catch (error) { text(useMessage, error.message); } finally { add.disabled = false; }
      });
      add.disabled = hidden;
      const use = document.createElement('div'); use.className = 'resource-use'; use.append(choose, add, useMessage); row.append(use);
      const personalVisibilityActions = actions(row);
      const visibilityButton = button(hidden ? '恢复显示' : '从我的数据列表移除', async () => {
        visibilityButton.disabled = true;
        try {
          const applied = await setPersonalDatasetVisibility(datasetId, !hidden);
          if (!applied) return;
          renderDatasets(datasetAttachState.rows, true);
          renderDerivedDatasets(derivedListState.rows);
        } catch (error) {
          visibilityButton.disabled = false;
          text(useMessage, `更新个人显示偏好失败：${error.message}`);
        }
      });
      personalVisibilityActions.append(visibilityButton);
      const files = catalogDatasetFiles(dataset);
      const whole = document.createElement('label'); whole.className = 'dataset-whole';
      const all = document.createElement('input'); all.type = 'checkbox'; all.disabled = !files.length || hidden;
      all.checked = selectedProjectDatasets.has(datasetId);
      all.onchange = () => { if (all.checked) selectedProjectDatasets.add(datasetId); else selectedProjectDatasets.delete(datasetId); updateProjectDatasetSelection(); };
      const wholeLabel = dataset.kind === 'directory'
        ? `加入完整目录数据集 · ${Number.isSafeInteger(dataset.file_count) ? dataset.file_count : '?'} 个文件，${datasetDisplaySize(dataset, files)}`
        : `加入完整数据集 · ${files.length} 个文件，${datasetDisplaySize(dataset, files)}`;
      whole.append(all, span(wholeLabel)); row.append(whole);
      if (!files.length) paragraph(row, '该数据集当前没有可引用文件。');
      const first = files[0];
      if (first && first.path !== '__researchkit_dataset_tree__') {
        const location = runnerDatasetPath(first);
        paragraph(row, `运行只读路径示例：${location}`, 'path');
        const example = document.createElement('pre'); example.className = 'dataset-example';
        text(example, `# 新建项目的运行时可同时在服务器 IDE 和 Web 使用\nwith context.open_dataset(${JSON.stringify(datasetId)}, ${JSON.stringify(first.path)}) as stream:\n    for chunk in iter(lambda: stream.read(1024 * 1024), b''):\n        pass  # 在这里处理每个数据块`);
        row.append(example);
      } else if (dataset.kind === 'directory') {
        paragraph(row, `运行时目录：/datasets/${datasetId}/<数据集内相对路径>；请按数据集说明填写实际文件路径。`, 'path');
        row.append(datasetFileBrowser(datasetId));
      }
      const advanced = document.createElement('details');
      const advancedSummary = document.createElement('summary'); text(advancedSummary, '高级：按文件加入下载清单'); advanced.append(advancedSummary);
      files.forEach(reference => {
        if (reference.path === '__researchkit_dataset_tree__') return;
        const label = document.createElement('label'); label.className = 'dataset-file';
        const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.disabled = hidden;
        const key = dataReferenceKey(reference);
        checkbox.checked = selectedDatasets.has(key);
        checkbox.onchange = () => {
          if (checkbox.checked) selectedDatasets.set(key, reference); else selectedDatasets.delete(key);
          const selected = updateDatasetSelection();
          if (!selected && checkbox.checked) {
            const warning = document.querySelector('#dataset-summary').textContent;
            selectedDatasets.delete(key); checkbox.checked = false; updateDatasetSelection();
            text(message, warning);
          }
        };
        const details = document.createElement('span');
        text(details, `${reference.path} · ${displayBytes(reference.size)} · ${reference.sha256}`);
        label.append(checkbox, details); advanced.append(label);
      });
      if (advanced.childElementCount > 1) row.append(advanced);
      const version = foldDetails('datasets', `dataset:${datasetId}`, `${dataset.owner_username ? '派生' : '原始'}数据 · ${dataset.version_label || dataset.name || datasetId} · ${datasetDisplaySize(dataset, files)}`, row, 'dataset-fold');
      const family = dataset.family_id || datasetId;
      if (!versionGroups.has(family)) {
        const group = document.createElement('details'); group.className = 'dataset-version-group'; group.open = true;
        const summary = document.createElement('summary'); text(summary, dataset.name || family); group.append(summary); versionGroups.set(family, group); list.append(group);
      }
      versionGroups.get(family).append(version);
    });
    updateDatasetSelection(); updateProjectDatasetSelection();
  }
  async function datasets() {
    const epoch = ++datasetEpoch;
    const identity = personalVisibilityIdentity();
    try {
      const [rows, projectsForAttach, visibility] = await Promise.all([
        api('/api/datasets'), api('/api/projects?include_archived=true'), api('/api/personal-resource-visibility'),
      ]);
      if (epoch !== datasetEpoch || identity !== personalVisibilityIdentity()) return;
      personalDatasetVisibility.identity = identity;
      personalDatasetVisibility.hidden = new Set(Array.isArray(visibility?.hidden_datasets) ? visibility.hidden_datasets : []);
      datasetAttachState.projects = Array.isArray(projectsForAttach) ? projectsForAttach : [];
      renderDatasets(Array.isArray(rows) ? rows : []);
      derivedDatasets();
    } catch (error) {
      if (epoch !== datasetEpoch) return;
      text(document.querySelector('#datasets-message'), error.message);
      document.querySelector('#dataset-list')?.replaceChildren();
      const selection = document.querySelector('#dataset-selection'); if (selection) selection.hidden = true;
    }
  }
  function renderDerivedDatasets(rows) {
    const section = document.querySelector('#derived-dataset-listing'), list = document.querySelector('#derived-dataset-list'), message = document.querySelector('#derived-datasets-message');
    if (!section || !list || !message) return;
    const personalRows = personalDatasetVisibility.showHidden ? rows : rows.filter(record => !datasetHidden(record?.dataset_id));
    section.hidden = !rows.length && !derivedListState.cursor; list.replaceChildren();
    if (!personalRows.length) {
      text(message, rows.length ? '当前页的派生数据已从本人列表移除；可勾选“显示已移除数据”恢复。' : '暂无可用派生数据。');
      if (derivedListState.cursor) {
        const more = button('加载更多派生数据', () => derivedDatasets(true));
        more.disabled = derivedListState.loading; list.append(more);
      }
      return;
    }
    text(message, '派生数据保存在托管数据区；项目只保存其固定版本引用。');
    personalRows.forEach(record => {
      const row = card(), files = catalogDatasetFiles(record), visibility = record.visibility || 'private';
      heading(row, `${record.name || record.dataset_id || '未命名派生数据'} · ${record.dataset_id || '数据集 ID 不可用'}`);
      paragraph(row, `所有者：${record.owner_username || '未知'} · 可见性：${({private: '仅本人可用', review: '教师审核可读（未入平台公共库）', public: '平台可复用'})[visibility] || visibility} · ${datasetDisplaySize(record, files)}`);
      if (record.created_at) paragraph(row, `保存时间：${record.created_at}`, 'path');
      if (record.source?.kind) paragraph(row, `来源：${record.source.kind === 'run' ? '后台运行产物' : '项目文件导入'}`, 'path');
      if (record.source?.submission_id) paragraph(row, `来源提交／试运行快照：${record.source.submission_id}`, 'path');
      if (record.source?.run_id) paragraph(row, `来源运行：${record.source.run_id}`, 'path');
      if (record.source?.verification === 'user-declared') paragraph(row, '导入来源由使用者声明；这不是算法成功运行的证据。');
      const controls = actions(row);
      const hidden = datasetHidden(record.dataset_id);
      const visibilityButton = button(hidden ? '恢复显示' : '从我的数据列表移除', async () => {
        visibilityButton.disabled = true;
        try {
          const applied = await setPersonalDatasetVisibility(record.dataset_id, !hidden);
          if (!applied) return;
          renderDatasets(datasetAttachState.rows, true);
          renderDerivedDatasets(derivedListState.rows);
        } catch (error) {
          visibilityButton.disabled = false;
          text(message, `更新个人显示偏好失败：${error.message}`);
        }
      });
      controls.append(visibilityButton);
      list.append(foldDetails('derived-datasets', `dataset:${record.dataset_id}`, `${record.owner_username || '未知账号'} · ${record.name || record.dataset_id} · ${datasetDisplaySize(record, files)}`, row));
    });
    if (derivedListState.cursor) {
      const more = button('加载更多派生数据', () => derivedDatasets(true));
      more.disabled = derivedListState.loading; list.append(more);
    }
  }
  async function derivedDatasets(append = false) {
    if (append && (!derivedListState.cursor || derivedListState.loading)) return;
    const epoch = ++derivedDatasetEpoch;
    derivedListState.loading = true;
    try {
      const value = await api('/api/derived-datasets?visibility=available&limit=50' + (append ? `&cursor=${encodeURIComponent(derivedListState.cursor)}` : ''));
      if (epoch !== derivedDatasetEpoch) return;
      const rows = Array.isArray(value?.items) ? value.items : [];
      derivedListState.rows = append ? [...derivedListState.rows, ...rows] : rows;
      derivedListState.cursor = value?.next_cursor || null;
      derivedListState.loading = false;
      renderDerivedDatasets(derivedListState.rows);
    } catch (error) {
      if (epoch !== derivedDatasetEpoch) return;
      const message = document.querySelector('#derived-datasets-message');
      if (message) text(message, error.message);
    } finally { if (epoch === derivedDatasetEpoch) derivedListState.loading = false; }
  }
  function reviewPath(projectId = '', cursor = '') {
    const query = new URLSearchParams();
    if (!projectId && reviewState.query) query.set('q', reviewState.query);
    query.set('limit', '20'); if (cursor) query.set('cursor', cursor);
    return projectId ? `/api/review/submission-projects/${encodeURIComponent(projectId)}/versions?${query}` : `/api/review/submission-projects?${query}`;
  }
  async function openReviewFile(submissionId, path) {
    try {
      const file = await api(`/api/review/submissions/${encodeURIComponent(submissionId)}/file?path=${encodeURIComponent(path)}`);
      if (file.binary) throw new Error('二进制内容不能在 Web 编辑器显示。');
      setEditor(`${submissionId} · ${path}`, {...file, submission_id: submissionId}, true);
    } catch (error) { text(output, error.message); }
  }
  async function renderReviewDetail(submissionId, host) {
    host.replaceChildren();
    try {
      const detail = await api(`/api/review/submissions/${encodeURIComponent(submissionId)}/release`);
      const submission = detail.submission || detail;
      heading(host, `${submission.project?.name || submission.project_name || '固定提交'} · ${submission.version || ''}`);
      paragraph(host, `来源账号：${ownerName(submission.project || submission)} · 提交摘要：${submission.content_sha256 || '不可用'}`, 'path');
      const testCopyBox = actions(host);
      const testCopyBtn = button('复制到 ly 工作区用 IDE 测试', async () => {
        testCopyBtn.disabled = true;
        try {
          const copied = await post(`/api/review/submissions/${encodeURIComponent(submissionId)}/test-copy`, {});
          paragraph(host, `已复制为测试项目：${copied.name || copied.project_id}（请在 IDE 中选择解释器运行，不会影响入库）`, 'action-result success');
          await projects();
        } catch (error) { paragraph(host, `复制测试失败：${error.message}`, 'action-result error'); testCopyBtn.disabled = false; }
      });
      testCopyBox.append(testCopyBtn);
      const files = submission.manifest?.files || detail.manifest?.files || [];
      if (files.length) fileList(host, files, path => openReviewFile(submissionId, path), true);
      const experimentsHost = document.createElement('div'); host.append(experimentsHost);
      experiments?.renderSubmission(experimentsHost, submissionId, {review: true});
      const publication = document.createElement('section'); publication.className = 'asset-review-form';
      heading(publication, '审核与规范入库');
      paragraph(publication, '统一填写标签与说明后入库；作为已有资源的新版本时，可继承原资源的标签与说明。发布不会修改来源项目。');
      window.ResearchKitReviewBatch.render(publication, detail, {submissionId, post, onPublished: loadReviewProjects});
      host.append(publication);
      const trial = document.createElement('form'); trial.className = 'asset-review-form'; heading(trial, '分发试用（未入库也可）');
      paragraph(trial, '试用只分发这份固定提交的副本，不会使其自动入库。');
      const targets = document.createElement('input'); targets.value = 'ly'; targets.maxLength = 1000;
      const targetLabel = document.createElement('label'); targetLabel.append(span('领取账号'), targets);
      const instructions = document.createElement('textarea'); instructions.maxLength = 4000;
      const instructionsLabel = document.createElement('label'); instructionsLabel.append(span('说明（可选）'), instructions);
      const submit = document.createElement('button'); submit.type = 'submit'; text(submit, '分发试用');
      const retry = {fingerprint: null, requestId: null};
      trial.append(targetLabel, instructionsLabel, submit);
      trial.onsubmit = async event => {
        event.preventDefault(); submit.disabled = true;
        try {
          const targetList = targetNames(targets.value); if (!targetList.length) throw new Error('请至少填写一个领取账号。');
          await post('/api/tasks/trial', idempotentRequest(retry, {submission_id: submissionId, targets: targetList, instructions: instructions.value.trim()}));
          paragraph(trial, '试用副本已提供；接收者可到“项目 → 教师任务与资源包”开始使用。');
          document.dispatchEvent(new CustomEvent('researchkit:tasks-changed'));
        } catch (error) { paragraph(trial, error.message); }
        finally { submit.disabled = false; }
      };
      host.append(trial);
    } catch (error) { paragraph(host, error.message); }
  }
  function renderReviewVersion(host, value) {
    const version = document.createElement('section'); version.className = 'review-submission';
    paragraph(version, `${value.version || '版本不可用'} · ${value.kind === 'major' ? '大版本' : '小版本'} · ${formatBeijingTime(value.created_at)}`);
    paragraph(version, value.submission_id, 'path');
    actions(version).append(button('查看审核与入库', () => { const detail = document.querySelector('#asset-detail'); detail.hidden = false; text(document.querySelector('#asset-detail-title'), `审核：${value.submission_id}`); renderReviewDetail(value.submission_id, document.querySelector('#asset-detail-content')); }));
    host.append(version);
  }
  function renderReviewProject(item) {
    const project = item.project || {}, id = project.project_id, row = card();
    heading(row, project.name || id || '未命名项目');
    paragraph(row, `来源账号：${ownerName(project)} · 匹配版本：${item.version_count ?? 0}`);
    if (item.preview?.submission_id) renderReviewVersion(row, item.preview);
    if ((item.version_count || 0) > 1) {
      const content = document.createElement('div');
      const versions = (reviewState.histories.get(id) || []).filter((value, index, all) => value?.submission_id && value.submission_id !== item.preview?.submission_id && all.findIndex(other => other?.submission_id === value.submission_id) === index);
      versions.forEach(value => renderReviewVersion(content, value));
      const loaded = reviewState.histories.has(id), cursor = reviewState.historyCursors.get(id);
      if (reviewState.historyLoading.has(id)) paragraph(content, '正在读取历史版本…');
      else if (!loaded || cursor) content.append(button(loaded ? '更多历史版本' : '读取历史版本', () => loadReviewHistory(id, loaded ? cursor : '')));
      const details = foldDetails('review', `history:${id}`, `更多版本（共 ${item.version_count} 个）`, content, 'review-history');
      const save = details.ontoggle;
      details.ontoggle = () => { save(); if (details.open && !reviewState.histories.has(id) && !reviewState.historyLoading.has(id)) loadReviewHistory(id); };
      row.append(details);
    }
    return row;
  }
  function renderReviewProjects() {
    const list = document.querySelector('#asset-candidate-list'); if (!list) return; list.replaceChildren();
    const query = String(reviewState.query || '').trim();
    const values = [...reviewState.projects.values()].filter(item => !query || textMatch(query, item.project?.name, item.project?.owner_username, item.preview?.submission_id, item.preview?.version, ...(reviewState.histories.get(item.project?.project_id) || []).flatMap(value => [value.version, value.submission_id])));
    if (!values.length) paragraph(list, reviewState.loading ? '正在读取固定提交…' : '没有符合条件的固定提交。');
    const groups = new Map();
    values.forEach(item => {
      const project = item.project || {}, key = `${project.owner_uid ?? ''}:${ownerName(project)}`;
      if (!groups.has(key)) groups.set(key, {name: ownerName(project), items: []});
      groups.get(key).items.push(item);
    });
    const keys = [...groups].flatMap(([key, group]) => [`owner:${key}`, ...group.items.filter(item => item.version_count > 1).map(item => `history:${item.project.project_id}`)]);
    foldControls(list, 'review', keys, renderReviewProjects);
    groups.forEach((group, key) => {
      group.items.sort((a, b) => String(b.latest_at || b.preview?.created_at || '').localeCompare(String(a.latest_at || a.preview?.created_at || '')) || String(b.project?.project_id || '').localeCompare(String(a.project?.project_id || '')));
      const host = document.createElement('section'); host.className = 'review-owner-group';
      heading(host, `来源账号：${group.name}`);
      paragraph(host, `当前已加载 ${group.items.length} 个项目；默认显示最近提交，其余可展开。`, 'asset-note');
      host.append(renderReviewProject(group.items[0]));
      if (group.items.length > 1) {
        const content = document.createElement('div'); content.className = 'cards';
        group.items.slice(1).forEach(item => content.append(renderReviewProject(item)));
        const more = foldDetails('review', `owner:${key}`, `更多提交（${group.items.length - 1} 个其他项目）`, content, 'review-owner-more');
        if (query) more.open = true;
        host.append(more);
      }
      list.append(host);
    });
    if (reviewState.cursor && !reviewState.loading) list.append(button('加载更多提交项目', () => loadReviewProjects(true)));
  }
  async function loadReviewHistory(projectId, cursor = '') {
    if (reviewState.historyLoading.has(projectId)) return;
    const epoch = reviewState.epoch, request = {};
    reviewState.historyLoading.set(projectId, request); renderReviewProjects();
    try {
      const value = await api(reviewPath(projectId, cursor));
      if (epoch !== reviewState.epoch || reviewState.historyLoading.get(projectId) !== request) return;
      const existing = cursor ? (reviewState.histories.get(projectId) || []) : [];
      reviewState.histories.set(projectId, [...existing, ...(value.items || [])]);
      reviewState.historyCursors.set(projectId, value.next_cursor || null);
    } catch (error) { if (epoch === reviewState.epoch) text(document.querySelector('#asset-message'), error.message); }
    finally { if (reviewState.historyLoading.get(projectId) === request) { reviewState.historyLoading.delete(projectId); renderReviewProjects(); } }
  }
  async function loadReviewProjects(append = false) {
    if (me?.role !== 'teacher') return;
    const epoch = ++reviewState.epoch; reviewState.loading = true; renderReviewProjects();
    try {
      const value = await api(reviewPath('', append ? reviewState.cursor : ''));
      if (epoch !== reviewState.epoch) return;
      if (!append) { reviewState.projects.clear(); reviewState.histories.clear(); reviewState.historyCursors.clear(); reviewState.historyLoading.clear(); }
      (value.items || []).forEach(item => { const id = item.project?.project_id; if (id) reviewState.projects.set(id, item); });
      reviewState.cursor = value.next_cursor || null;
    } catch (error) { if (epoch === reviewState.epoch) text(document.querySelector('#asset-message'), error.message); }
    finally { if (epoch === reviewState.epoch) { reviewState.loading = false; renderReviewProjects(); } }
  }
  async function componentAccess() {
    const list = document.querySelector('#component-access-list'), message = document.querySelector('#component-access-message');
    const epoch = ++componentAccessState.epoch;
    const rows = await api(`/api/component-access${componentAccessState.includeInactive ? '?include_inactive=true' : ''}`);
    if (epoch !== componentAccessState.epoch) return;
    list.replaceChildren();
    if (!rows.length) paragraph(list, '没有可配置的算法或模板。');
    rows.forEach(row => {
      const cardNode = card(); heading(cardNode, `${row.kind === 'template' ? '模板' : '算法'} · ${row.ref}`);
      if (row.lifecycle?.state && row.lifecycle.state !== 'active') paragraph(cardNode, `状态：${window.ResearchKitLifecycle?.STATE_LABELS?.[row.lifecycle.state] || row.lifecycle.state}。`);
      const form = document.createElement('form'); form.className = 'access-form';
      const publicBox = document.createElement('input'); publicBox.type = 'checkbox'; publicBox.checked = Boolean(row.public);
      const publicLabel = document.createElement('label'); publicLabel.className = 'access-checkbox'; publicLabel.append(publicBox, span('公开给全部获准 Web 用户'));
      const targets = document.createElement('input'); targets.value = (row.targets || []).map(target => target.username).filter(Boolean).join(', '); targets.placeholder = '账号名，使用逗号分隔';
      const targetLabel = document.createElement('label'); targetLabel.append(span('额外授权账号'), targets);
      const save = document.createElement('button'); save.type = 'submit'; text(save, '保存权限'); form.append(publicLabel, targetLabel, save);
      form.onsubmit = async event => {
        event.preventDefault(); save.disabled = publicBox.disabled = targets.disabled = true;
        try {
          const updated = await api('/api/component-access', {method: 'PUT', body: JSON.stringify({kind: row.kind, ref: row.ref, public: publicBox.checked, targets: targetNames(targets.value), expected_revision: row.revision})});
          row = updated; publicBox.checked = Boolean(updated.public); targets.value = (updated.targets || []).map(target => target.username).filter(Boolean).join(', ');
          text(message, '共享权限已保存。撤销不会删除已有项目副本。'); await templates();
        } catch (error) {
          text(message, error.status === 409 ? `权限记录已变化：${error.message}。当前输入仍保留，请重新读取后再决定。` : error.message);
        } finally { save.disabled = publicBox.disabled = targets.disabled = false; }
      };
      cardNode.append(form);
      window.ResearchKitLifecycle?.append(cardNode, {
        kind: row.kind, ref: row.ref, lifecycle: row.lifecycle, api,
        message: value => text(message, value), onChanged: async () => { await componentAccess(); await templates(); },
      });
      list.append(cardNode);
    });
  }
  function taskProjectSelect(task) {
    const select = document.createElement('select');
    const blank = document.createElement('option'); blank.value = ''; text(blank, '选择自己的已有项目'); select.append(blank);
    projectState.rows.filter(project => !project.archived && !project.unavailable).forEach(project => {
      const option = document.createElement('option'); option.value = project.project_id; text(option, project.name || project.project_id);
      select.append(option);
    });
    select.value = task.project_id || task.claimed_project_id || task.workspace_project_id || '';
    return select;
  }
  async function taskMaterials(task, host) {
    try {
      const detail = await api(`/api/tasks/${encodeURIComponent(task.task_id)}/materials?personal=true`);
      const panel = document.createElement('section'); panel.className = 'task-material-panel';
      paragraph(panel, '以下资源已经在你的可用范围内，不需要领取。加入项目只添加缺少的材料，不覆盖代码或已有算法配置。');
      (detail.materials || []).forEach(item => paragraph(panel, `${item.kind === 'algorithm' ? '算法' : item.kind === 'dataset' ? '数据' : '案例'} · ${item.ref}`));
      (detail.supplements || []).forEach(item => {
        heading(panel, `教师补充材料 · ${formatBeijingTime(item.created_at)}`);
        if (item.instructions) markdown(panel, item.instructions, 'task-supplement-instructions');
        (detail.materials || []).filter(material => material.supplement_id === item.supplement_id).forEach(material => paragraph(panel, `本次补充：${material.ref}`));
      });
      const target = taskProjectSelect({...task, ...detail}); panel.append(target);
      const add = button('添加材料到所选项目', () => applyTaskToProject(task, target.value, panel, add));
      panel.append(add); host.append(panel);
    } catch (error) { paragraph(host, `读取材料失败：${error.message}`); }
  }
  async function applyTaskToProject(task, projectId, host, trigger) {
    if (!projectId) { paragraph(host, '请选择自己的项目；含案例的任务可先点“开始任务”。'); return; }
    trigger.disabled = true;
    try {
      await post(`/api/projects/${encodeURIComponent(projectId)}/task-materials`, {task_id: task.task_id});
      paragraph(host, '材料已处理。', 'action-result success'); await projects();
    } catch (error) { paragraph(host, error.message, 'action-result error'); }
    finally { trigger.disabled = false; }
  }
  async function supplementTask(task, host) {
    const panel = document.createElement('form'); panel.className = 'asset-review-form'; heading(panel, '补充资源');
    paragraph(panel, '可只补充给部分学生；补充资源不会重建项目或覆盖他们已经修改的代码。');
    const algorithms = document.createElement('input'); algorithms.placeholder = '额外算法引用，用逗号分隔（可选）';
    const datasets = document.createElement('input'); datasets.placeholder = '额外数据集 ID，用逗号分隔（可选）';
    const targets = document.createElement('input'); targets.placeholder = '留空表示原任务全部学生；或填写 ly, test';
    const instructions = document.createElement('textarea'); instructions.placeholder = '补充说明（可选）';
    [['额外算法工具', algorithms], ['额外数据材料', datasets], ['接收账号', targets], ['说明', instructions]].forEach(([label, field]) => { const node = document.createElement('label'); node.append(span(label), field); panel.append(node); });
    const submit = document.createElement('button'); submit.type = 'submit'; text(submit, '确认补充'); panel.append(submit);
    const retry = {fingerprint: null, requestId: null};
    panel.onsubmit = async event => {
      event.preventDefault(); submit.disabled = true;
      try {
        await post(`/api/tasks/${encodeURIComponent(task.task_id)}/supplements`, idempotentRequest(retry, {algorithm_refs: targetNames(algorithms.value), dataset_ids: targetNames(datasets.value), targets: targetNames(targets.value), instructions: instructions.value.trim()}));
        const outcome = `补充成功：“${task.name || task.task_id}”。学生在原任务中查看并领取补充资源。`;
        taskState.manager.notice = {taskId: task.task_id, text: outcome};
        paragraph(panel, outcome, 'action-result success');
        await tasks('manager');
      } catch (error) { paragraph(panel, error.message); }
      finally { submit.disabled = false; }
    };
    host.append(panel);
  }
  async function openTaskProject(projectId) {
    await projects();
    if (!projectState.rows.some(item => item.project_id === projectId)) {
      projectState.includeArchived = true;
      const include = document.querySelector('#project-include-archived'); if (include) include.checked = true;
      await projects();
    }
    const project = projectState.rows.find(item => item.project_id === projectId);
    if (!project) throw new Error('项目不在当前工作区，请刷新或检查个人项目目录。');
    projectState.query = projectId;
    const query = document.querySelector('#project-query'); if (query) query.value = projectId;
    saveFoldOpen('projects', `source:${sourceLabel(project)}`, true);
    saveFoldOpen('projects', `project:${projectId}`, true);
    const task = taskState.personal.items.find(item => item.project_id === projectId); if (task) saveFoldOpen('projects', `task:${task.family_id || task.task_id}`, true);
    renderProjects(); showPage('projects');
    text(output, `项目已就绪：${project.path || projectId}${project.archived ? '（已归档，可点击恢复项目）' : ''}`);
  }
  async function claimTask(task, trigger, message) {
    trigger.disabled = true;
    try {
      paragraph(message, '正在准备本任务版本的个人项目…');
      const result = await post(`/api/tasks/${encodeURIComponent(task.task_id)}/claim`, {});
      await tasks('personal');
      await openTaskProject(result.project?.project_id || result.project_id);
    } catch (error) { paragraph(message, `项目准备未完成：${error.message}。可重试；已有项目及个人修改会保留。`); }
    finally { trigger.disabled = false; }
  }
  function renderTasks(scope) {
    const teacherView = scope === 'manager';
    const state = taskState[scope];
    const list = document.querySelector(teacherView ? '#teacher-task-list' : '#task-list'); if (!list) return; list.replaceChildren();
    const families = new Map();
    const ordered = [...state.items].sort((a,b) => Number(b.task_version || 1) - Number(a.task_version || 1));
    const message = document.querySelector(teacherView ? '#teacher-task-message' : '#task-message');
    if (message) {
      message.className = `asset-message${teacherView && state.notice ? ' action-result success' : ''}`;
      text(message, teacherView ? state.notice?.text || '' : '');
    }
    if (!state.items.length) { paragraph(list, teacherView ? '当前没有已布置任务。' : '当前没有任务或补充材料。'); return; }
    const visibleTasks = ordered.filter(task => teacherView || textMatch(distributionState.query, task.name, task.task_id, task.teacher_username, task.baseline_submission_id, task.kind === 'trial' ? '试用' : '正式', task.delivery_mode === 'resources' ? '资源包' : '完整任务'));
    if (teacherView) {
      const recipientsFor = task => Array.isArray(task.recipients) && task.recipients.length
        ? task.recipients.map(recipient => ({...recipient, username: recipient.username || recipient.target_username || '未知账号'}))
        : [{username: '未指定接收者'}];
      const recipientStarted = recipient => Boolean(recipient.project_id || recipient.claimed_project_id || recipient.workspace_project_id
        || recipient.claim_status === 'claimed' || ['claimed', 'started', 'complete', 'completed'].includes(String(recipient.status || '').toLowerCase()));
      const recipientLabel = recipient => `${recipient.username}${recipientStarted(recipient) ? '（已开始）' : '（未开始）'}`;
      const recipientGroups = new Map();
      visibleTasks.forEach(task => {
        recipientsFor(task).forEach(recipient => {
          const uname = recipient.username;
          if (!recipientGroups.has(uname)) recipientGroups.set(uname, []);
          recipientGroups.get(uname).push({task, recipient});
        });
      });
      recipientGroups.forEach((groupItems, uname) => {
        const group = document.createElement('details'); group.className = 'task-recipient-group'; group.open = false;
        const summary = document.createElement('summary'); text(summary, `接收者：${uname} · ${groupItems.length} 个任务`); group.append(summary);
        groupItems.forEach(({task, recipient}) => {
          const item = document.createElement('details'); item.className = 'distributed-task'; item.open = false;
          const taskType = task.legacy_distribution ? '旧版分发' : task.delivery_mode === 'resources' ? '资源包' : task.kind === 'trial' ? '试用任务' : '完整任务';
          const title = document.createElement('summary'); text(title, `${taskType} · ${task.name || task.task_id}（${task.task_id}）`); item.append(title);
          const row = card();
          paragraph(row, task.task_id, 'path');
          heading(row, '任务说明'); markdown(row, task.instructions || '暂无说明。', 'task-instructions');
          if (task.baseline_submission_id) paragraph(row, `来源固定提交：${task.baseline_submission_id}`, 'path');
          paragraph(row, `包含材料：${Array.isArray(task.materials) && task.materials.length ? task.materials.map(item => item.ref || item.dataset_id).join('、') : '无额外材料'}`);
          paragraph(row, `该用户开始状态：${recipientStarted(recipient) ? '已开始' : '未开始'}`);
          paragraph(row, `全部接收人：${recipientsFor(task).map(recipientLabel).join('、')}`);
          if (state.notice?.taskId === task.task_id) paragraph(row, state.notice.text, 'action-result success');
          const controls = actions(row);
          const delBtn = button('删除任务', () => {
            delBtn.hidden = true;
            let deleting = false;
            const confirmBox = document.createElement('div'); confirmBox.className = 'distributed-task-confirm';
            paragraph(confirmBox, `确认删除“${task.name || task.task_id}”的分发记录吗？全部接收人：${recipientsFor(task).map(recipientLabel).join('、')}。`);
            paragraph(confirmBox, '这只删除分发记录，不删除平台资源和学生已有项目；算法和数据授权保留。');
            const confirmActions = actions(confirmBox);
            const confirmBtn = button('确认删除任务', async () => {
              if (deleting) return;
              deleting = true;
              confirmBtn.disabled = true; cancelBtn.disabled = true;
              try {
                await post(`/api/tasks/${encodeURIComponent(task.task_id)}/delete`, {});
                taskState.manager.notice = {taskId: task.task_id, text: `已删除任务：${task.name || task.task_id}。分发记录已删除；平台资源、学生已有项目和算法/数据授权保留。`};
                await refreshDistributedTasks();
              } catch (error) {
                paragraph(confirmBox, error.message, 'action-result error');
                deleting = false;
                confirmBtn.disabled = false; cancelBtn.disabled = false;
              }
            });
            const cancelBtn = button('取消', () => { confirmBox.remove(); delBtn.hidden = false; });
            confirmActions.append(confirmBtn, cancelBtn); row.append(confirmBox);
          });
          delBtn.className = 'danger-btn';
          controls.append(delBtn);
          item.append(row); group.append(item);
        });
        list.append(group);
      });
      return;
    }
    visibleTasks.forEach(task => {
      const row = card(); heading(row, `${task.delivery_mode === 'resources' ? '资源包' : task.kind === 'trial' ? '试用任务' : '完整任务'} · ${task.name || task.task_id}`);
      paragraph(row, task.task_id, 'path');
      if (task.instructions) markdown(row, task.instructions, 'task-instructions');
      if (Number(task.supplement_count) > 0) paragraph(row, `教师补充了 ${task.supplement_count} 批材料${task.latest_supplement_at ? ` · 最近 ${formatBeijingTime(task.latest_supplement_at)}` : ''}。可直接添加到本任务项目，详情中保留补充说明。`, 'action-result');
      if (task.baseline_submission_id) paragraph(row, `来源固定提交：${task.baseline_submission_id}`, 'path');
      const controls = actions(row), messageHost = document.createElement('div'), materialHost = document.createElement('div');
      const claimed = task.claim_status === 'claimed' && task.project_id;
      const resources = task.delivery_mode === 'resources';
      const deletedByTeacher = Boolean(task.deleted_by_teacher || task.deleted_at);
      if (deletedByTeacher && !claimed && !resources) {
        paragraph(row, '该布置任务已被教师删除，无法再从此入口开始任务；已开始的研究项目和独立资源权限不受影响。', 'data-management-warning');
        const dismissBtn = button('删除记录', async () => {
          dismissBtn.disabled = true;
          try { await post(`/api/tasks/${encodeURIComponent(task.task_id)}/dismiss`, {}); await tasks('personal'); }
          catch (error) { paragraph(messageHost, error.message); dismissBtn.disabled = false; }
        });
        controls.append(dismissBtn);
      } else {
        paragraph(row, resources ? '算法和数据已自动成为可用资源，可在算法库、数据资源中使用，也可在这里加入已有项目。' : claimed ? '这个任务版本已有个人研究副本。' : '点击开始任务后创建自己的研究副本；同一版本不会重复创建。', 'asset-note');
        if (!resources) {
          const primary = button(claimed ? '打开项目' : '开始任务', async () => {
            if (claimed) { primary.disabled = true; try { await openTaskProject(task.project_id); } catch (error) { paragraph(messageHost, error.message); } finally { primary.disabled = false; } }
            else await claimTask(task, primary, messageHost);
          }); controls.append(primary);
          if (claimed) {
            const add = button('添加材料到本任务项目', () => applyTaskToProject(task, task.project_id, messageHost, add)); controls.append(add);
          }
        } else {
          const target = taskProjectSelect(task); row.append(target);
          const add = button('添加材料到项目', () => applyTaskToProject(task, target.value, messageHost, add)); controls.append(add);
        }
        const materials = button('查看任务材料与补充说明', async () => {
          materials.disabled = true; materialHost.replaceChildren();
          try { await projects(); await taskMaterials(task, materialHost); } catch (error) { paragraph(materialHost, error.message); }
          finally { materials.disabled = false; }
        }); controls.append(materials);
        if (deletedByTeacher) {
          const dismissBtn = button('删除记录', async () => {
            dismissBtn.disabled = true;
            try { await post(`/api/tasks/${encodeURIComponent(task.task_id)}/dismiss`, {}); await tasks('personal'); }
            catch (error) { paragraph(messageHost, error.message); dismissBtn.disabled = false; }
          });
          controls.append(dismissBtn);
        }
      }
      row.append(messageHost, materialHost);
      const familyId = task.family_id || task.task_id;
      if (!families.has(familyId)) {
        const group = document.createElement('details'); group.className = 'task-family'; group.open = true;
        const summary = document.createElement('summary'); text(summary, task.name || familyId); group.append(summary);
        families.set(familyId, group); list.append(group);
      }
      const version = document.createElement('details'); version.className = 'task-version';
      version.open = !families.get(familyId).dataset.hasVersion;
      const title = document.createElement('summary'); text(title, `任务版本 ${task.task_version || 1}${version.open && ordered.some(other => other.task_id !== task.task_id && (other.family_id || other.task_id) === familyId) ? ' · 有新版本可选' : ''}`);
      version.append(title, row); families.get(familyId).append(version); families.get(familyId).dataset.hasVersion = 'true';
    });
  }
  async function tasks(scope = 'personal') {
    if (scope === 'manager' && me?.role !== 'teacher') return;
    const state = taskState[scope];
    const epoch = ++state.epoch;
    try {
      const value = await api(scope === 'manager' ? '/api/tasks' : '/api/tasks?personal=true'); if (epoch !== state.epoch) return;
      state.items = Array.isArray(value?.items) ? value.items : (Array.isArray(value) ? value : []); renderTasks(scope); if (scope === 'personal') { renderDistributions(); renderProjects(); }
    } catch (error) {
      const message = document.querySelector(scope === 'manager' ? '#teacher-task-message' : '#task-message');
      if (message) {
        if (scope === 'manager' && state.notice) {
          message.className = 'asset-message action-result error';
          text(message, `${state.notice.text} 列表刷新失败，请刷新重试。`);
        } else {
          message.className = 'asset-message action-result error'; text(message, error.message);
        }
      }
    }
  }
  async function refreshDistributedTasks() {
    const results = await Promise.allSettled([tasks('manager'), tasks('personal'), distributions()]);
    if (results.some(result => result.status === 'rejected')) {
      const message = document.querySelector('#teacher-task-message');
      if (message && taskState.manager.notice) {
        message.className = 'asset-message action-result error';
        text(message, `${taskState.manager.notice.text} 列表刷新失败，请刷新重试。`);
      }
    }
  }
  let currentPage = 'projects';
  let currentManagementModule = 'template';
  function syncNavAriaCurrent() {
    const navPage = ['data-management', 'retired-bin', 'cleanup-bin'].includes(currentPage) ? 'resource-management' : currentPage;
    document.querySelectorAll('[data-page]').forEach(node => {
      let active = node.dataset?.page === navPage;
      if (active && node.dataset?.page === 'resource-management') {
        const nodeMod = node.dataset?.module || '';
        active = Boolean(nodeMod && nodeMod === currentManagementModule);
      }
      node.setAttribute('aria-current', active ? 'page' : 'false');
    });
  }
  function awaitlessProjects() { projects().then(() => { if (currentPage === 'resource-library') window.ResearchKitAssignments?.loadLibrary(); }).catch(error => text(output, error.message)); }
  function showPage(page, moduleName) {
    if (page === 'distributions') page = 'projects';
    if (page === 'assignments') page = 'distributed-tasks';
    const managerPages = ['resource-management', 'data-management', 'retired-bin', 'cleanup-bin', 'asset-candidates', 'assignments', 'distributed-tasks'];
    if (managerPages.includes(page) && me?.role !== 'teacher') return;
    currentPage = page;
    if (typeof moduleName === 'string' && ['template', 'algorithm', 'dataset', 'all'].includes(moduleName)) {
      currentManagementModule = moduleName;
    } else if (['data-management', 'retired-bin', 'cleanup-bin'].includes(page)) {
      currentManagementModule = 'dataset';
    }
    syncNavAriaCurrent();
    if (page !== 'feedback') window.ResearchKitFeedback?.leave();
    const fileEditor = document.querySelector('section.editor');
    if (fileEditor) fileEditor.hidden = page === 'feedback' || page === 'distributed-tasks';
    if (!['resource-library', 'resource-management'].includes(page)) window.ResearchKitAssignments?.leaveLibrary?.();
    if (page !== 'data-management') window.ResearchKitDataManager?.leave();
    if (page !== 'retired-bin' && page !== 'cleanup-bin') window.ResearchKitCleanup?.leave();
    if (page !== 'datasets') { datasetEpoch += 1; derivedDatasetEpoch += 1; }
    ['projects', 'composer', 'versions', 'datasets', 'data-management', 'asset-candidates', 'distributions', 'resource-library', 'assignments', 'distributed-tasks', 'resource-management', 'component-access', 'retired-bin', 'cleanup-bin', 'feedback'].forEach(id => { const node = document.querySelector('#' + id); if (node) node.hidden = id !== page && !(page === 'projects' && id === 'distributions'); });
    if (page === 'feedback') window.ResearchKitFeedback?.enter();
    if (page === 'data-management') window.ResearchKitDataManager?.enter();
    if (page === 'retired-bin') window.ResearchKitCleanup?.enter('retired');
    if (page === 'cleanup-bin') window.ResearchKitCleanup?.enter('trashed');
  }
  function openComposer(projectId, algorithmRef) {
    if (dirty && !confirm('当前文本编辑器有未保存内容。它会保留在编辑器草稿中，但编排读取磁盘内容；若生成 case.py 会替换磁盘入口。请先保存，或明确保留草稿后继续。')) return;
    showPage('composer');
    window.ResearchKitComposer?.open(projectId, algorithmRef);
  }
  document.addEventListener('researchkit:open-composer', event => { openComposer(event.detail?.projectId, event.detail?.algorithmRef); });
  document.addEventListener('researchkit:show-projects', () => { showPage('projects'); });
  document.addEventListener('researchkit:distributions-changed', () => { distributions().catch(error => { text(output, error.message); }); });
  document.addEventListener('researchkit:tasks-changed', () => { tasks('personal'); });
  document.addEventListener('researchkit:show-cleanup', event => { if (me?.role === 'teacher') showPage(event.detail?.state === 'retired' ? 'retired-bin' : 'cleanup-bin'); });
  document.addEventListener('researchkit:projects-changed', () => { projects().catch(error => { text(output, error.message); }); });
  document.addEventListener('researchkit:management-module-changed', event => {
    const mod = event.detail?.module;
    if (typeof mod === 'string' && ['template', 'algorithm', 'dataset', 'all'].includes(mod)) {
      currentManagementModule = mod;
      if (currentPage === 'resource-management') syncNavAriaCurrent();
    }
  });
  document.querySelectorAll('[data-page]').forEach(node => { node.onclick = () => {
    const page = node.dataset.page;
    const mod = node.dataset?.module;
    if (page === 'composer') {
      if (dirty && !confirm('当前文本编辑器有未保存内容。它会保留在编辑器草稿中，但编排读取磁盘内容；若生成 case.py 会替换磁盘入口。请先保存，或明确保留草稿后继续。')) return;
      showPage(page); window.ResearchKitComposer?.show();
    } else {
      showPage(page, mod);
      if (page === 'datasets') datasets();
      if (page === 'resource-library') awaitlessProjects();
      if (page === 'resource-management') {
        if (node.dataset?.module) window.ResearchKitAssignments?.setManagementModule?.(node.dataset.module);
        window.ResearchKitAssignments?.loadManagement();
      }
      if (page === 'projects') { tasks('personal'); distributions().catch(error => { text(output, error.message); }); }
      if (page === 'distributed-tasks') refreshDistributedTasks();
      if (page === 'data-management') return;
      if (page === 'asset-candidates') loadReviewProjects();
      if (page === 'distributions') { distributions().catch(error => { text(output, error.message); }); tasks('personal'); }
    }
  }; });
  const submissionFilterForm = document.querySelector('#submission-filters');
  if (submissionFilterForm) {
    submissionFilterForm.onsubmit = async event => { event.preventDefault(); await versions(); };
    const clear = document.querySelector('#submission-filter-clear');
    if (clear) clear.onclick = async () => { submissionFilterForm.reset(); await versions(); };
    const refresh = document.querySelector('#submission-refresh');
    if (refresh) refresh.onclick = async () => { await versions(); };
  }
  const projectQuery = document.querySelector('#project-query');
  if (projectQuery) projectQuery.oninput = () => { projectState.query = projectQuery.value; renderProjects(); };
  const projectArchived = document.querySelector('#project-include-archived');
  if (projectArchived) projectArchived.onchange = async () => {
    projectState.includeArchived = Boolean(projectArchived.checked); saveProjectArchiveFilter(projectState.includeArchived);
    try { await projects(); } catch (error) { text(output, error.message); }
  };
  const distributionQuery = document.querySelector('#distribution-query');
  if (distributionQuery) distributionQuery.oninput = () => { distributionState.query = distributionQuery.value; renderDistributions(); renderTasks('personal'); };
  const distributedTasksRefresh = document.querySelector('#distributed-tasks-refresh');
  if (distributedTasksRefresh) distributedTasksRefresh.onclick = () => refreshDistributedTasks();
  const assetQuery = document.querySelector('#asset-candidate-query');
  if (assetQuery) assetQuery.oninput = () => {
    reviewState.query = assetQuery.value; renderReviewProjects();
    if (reviewQueryTimer !== null) clearTimeout(reviewQueryTimer);
    reviewQueryTimer = setTimeout(() => { reviewQueryTimer = null; loadReviewProjects(); }, 250);
  };
  const componentInactive = document.querySelector('#component-access-include-inactive');
  if (componentInactive) componentInactive.onchange = () => {
    componentAccessState.includeInactive = Boolean(componentInactive.checked);
    componentAccess().catch(error => { text(document.querySelector('#component-access-message'), error.message); });
  };
  updateCompareStatus();
  if (!bearer) text(notice, '缺少启动令牌。请使用终端打印的完整地址打开。');
  else api('/api/me').then(async value => {
    me = value; text(notice, `${me.username} 的${me.role === 'teacher' ? '教师' : '学生'}工作区 · v0.8（IDE实验方案与产物 · 最新结果与中文案例 · 20261116）`);
    projectState.includeArchived = projectArchiveFilter();
    if (projectArchived) projectArchived.checked = projectState.includeArchived;
    document.querySelector('#demo').hidden = !me.demo;
    document.querySelector('#asset-candidates-nav').hidden = me.role !== 'teacher';
    // These legacy maintenance pages stay in the DOM for existing records and
    // browser history, but are no longer normal teacher-management entrances.
    // The dataset registration button is shown by workflow.js only in 数据管理.
    document.querySelector('#data-management-nav').hidden = true;
    document.querySelector('#retired-bin-nav').hidden = true;
    document.querySelector('#cleanup-bin-nav').hidden = true;
    const teacherNavGroup = document.querySelector('#teacher-nav-group');
    if (teacherNavGroup) teacherNavGroup.hidden = me.role !== 'teacher';
    const resourceManagementNav = document.querySelector('#resource-management-nav'); if (resourceManagementNav) resourceManagementNav.hidden = true;
    ['#cases-management-nav', '#algorithms-management-nav', '#datasets-management-nav'].forEach(sel => {
      const btn = document.querySelector(sel); if (btn) btn.hidden = me.role !== 'teacher';
    });
    const assignmentsNav = document.querySelector('#assignments-nav');
    if (assignmentsNav) assignmentsNav.hidden = true;
    const distributedTasksNav = document.querySelector('#distributed-tasks-nav');
    if (distributedTasksNav) distributedTasksNav.hidden = me.role !== 'teacher';
    window.ResearchKitAssignments?.init({api, post, getMe: () => me, getProjects: () => projectState.rows});
    window.ResearchKitDataManager?.init({api, post, getMe: () => me});
    window.ResearchKitCleanup?.configure({api, post, getMe: () => me});
    window.ResearchKitFeedback?.configure({api, post, getMe: () => me});
    await Promise.all([projects(), versions(), distributions(), tasks('personal'), templates()]);
  }).catch(error => { text(notice, error.message); });
  const dataDownload = document.querySelector('#download-data-references');
  if (dataDownload) dataDownload.onclick = downloadDataReferences;
  const datasetQuery = document.querySelector('#dataset-query');
  if (datasetQuery) datasetQuery.oninput = () => renderDatasets(datasetAttachState.rows, true);
  const datasetProject = document.querySelector('#dataset-project');
  if (datasetProject) datasetProject.onchange = async () => {
    datasetAttachState.projectId = datasetProject.value;
    await loadProjectDatasetReferences(datasetAttachState.projectId);
  };
  const addProjectDatasets = document.querySelector('#add-project-datasets');
  if (addProjectDatasets) addProjectDatasets.onclick = attachProjectDatasets;
  const derivedProjectFile = document.querySelector('#derived-project-file');
  if (derivedProjectFile) derivedProjectFile.onchange = updateDerivedProjectUpload;
  const derivedProjectName = document.querySelector('#derived-project-name');
  if (derivedProjectName) derivedProjectName.oninput = updateDerivedProjectUpload;
  const saveProjectDerivedButton = document.querySelector('#save-project-derived');
  if (saveProjectDerivedButton) saveProjectDerivedButton.onclick = saveProjectDerived;
  if (window.__researchkitAppTest) window.__researchkitAppTest = {showPage, datasets, derivedDatasets, loadProjectDatasetReferences, saveProjectDerived, sourceLabel, targetNames, packageExport, importPackage, versions, loadSubmissionProjects, loadSubmissionHistory, submissionProjectsPath, formatBeijingTime, updateCompareStatus, setEditor, dataReference, dataReferences, runnerDatasetPath, renderDatasets, catalogDatasetFiles, projectDatasetSummary, attachProjectDatasets, assetPayload, assetDigest, renderAssetDetail, openAssetCandidate, openAssetFile, publishAsset, assetCandidates, projects, renderProjects, distributions, renderDistributions, archiveProject, componentAccess, componentAccessState, foldStorageKey, foldOpen, saveFoldOpen, candidateOrigin, submit, renderSubmissionConfirmation, renderPreparation, loadReviewProjects, renderReviewProjects, renderReviewDetail, tasks, renderTasks, taskMaterials, applyTaskToProject, claimTask, openTaskProject, loadReviewHistory, idempotentRequest, setMe: value => { me = value; }, setSelectedAsset: value => { selectedAssetId = value; }};
})();
