(() => {
  'use strict';

  const METADATA_FIELDS = ['name', 'description', 'wiki_ref', 'wiki_fact_state', 'provenance_ref', 'format', 'usage', 'license', 'student_conditions'];
  const EDITABLE_FIELDS = ['name', 'description', 'usage', 'format', 'license', 'student_conditions'];
  const EVIDENCE_FIELDS = ['wiki_ref', 'wiki_fact_state', 'provenance_ref'];
  const FIELD_LABELS = {
    name: '显示名称', description: '中文说明', usage: '建议用途', format: '数据格式',
    license: '使用条件', student_conditions: '学生使用条件', wiki_ref: '知识库索引',
    wiki_fact_state: '知识库说明状态', provenance_ref: '说明出处',
  };
  const STATUS_LABELS = {
    pending: '待审核', queued: '等待登记', publishing: '正在登记', published: '已登记',
    failed: '需要处理', ignored: '暂不纳入', withdrawn: '自动收起（历史）', unchanged: '没有变化',
  };
  const KIND_LABELS = {
    new: '新增目录', 'boundary-change': '数据集边界已更新', 'metadata-change': '说明更新', 'data-change': '目录或文件属性有变化', missing: '原目录不可读', error: '目录需要处理', unchanged: '没有变化',
  };
  const state = {
    context: null, controlsBound: false, active: false, autoScanRequested: false, scanInFlight: false,
    status: null, filter: 'pending', query: '', tags: [], tagMode: 'all', tagsLoading: false, tagsError: false, offset: 0, limit: 50, page: null,
    listEpoch: 0, detailEpoch: 0, selectedId: null, selectedDetail: null, drafts: new Map(), accessDrafts: new Map(), readmeOpen: new Set(), pollTimer: null, actionsDisabledByService: false, includeInactive: false,
  };

  const q = selector => document.querySelector(selector);
  const setText = (node, value) => { if (node) node.textContent = value == null ? '' : String(value); };
  const element = (tag, className = '') => { const node = document.createElement(tag); node.className = className; return node; };
  const appendText = (parent, tag, value, className = '') => { const node = element(tag, className); setText(node, value); parent.append(node); return node; };
  const button = (title, action, className = '') => { const node = element('button', className); node.type = 'button'; setText(node, title); node.onclick = action; return node; };
  const paragraph = (parent, value, className = '') => appendText(parent, 'p', value, className);
  const markdown = (parent, value, className = '') => {
    if (window.ResearchKitMarkdown?.append) return window.ResearchKitMarkdown.append(parent, value, {className, empty: '暂无说明。'});
    return paragraph(parent, value || '暂无说明。', className);
  };
  const clone = value => JSON.parse(JSON.stringify(value));
  const usable = () => Boolean(state.status?.configured && state.status?.worker_online);
  const api = (path, options) => state.context.api(path, options);
  const post = (path, body) => state.context.post(path, body);

  function renderDiagnostic(parent, record) {
    if (record.status === 'withdrawn') {
      if (record.error) paragraph(parent, record.error, 'data-management-note');
      return;
    }
    if (record.kind === 'data-change') {
      paragraph(parent, '扫描发现目录结构或文件属性与登记时不同；这并不一定意味着文件内容改变。当前不能覆盖已登记版本，请先核实并保留旧版本。', 'data-management-warning');
      if (typeof record.error === 'string' && record.error) paragraph(parent, `扫描详情：${record.error}`, 'data-management-error');
      else paragraph(parent, '扫描只比较目录结构和文件属性，未比较文件内容。可重新扫描以获取详情。', 'data-management-note');
    } else if (record.kind === 'missing' || record.kind === 'error') {
      paragraph(parent, record.kind === 'missing' ? '原目录暂时无法找到，请检查目录是否存在及服务是否能够访问。' : '扫描目录时遇到问题，请根据提示检查。', 'data-management-warning');
      if (typeof record.error === 'string' && record.error) paragraph(parent, `处理提示：${record.error}`, 'data-management-error');
    } else if (typeof record.error === 'string' && record.error) {
      paragraph(parent, `处理提示：${record.error}`, 'data-management-error');
    }
  }

  function metadataValue(metadata, key) {
    const value = metadata && metadata[key];
    return typeof value === 'string' ? value : '';
  }
  function candidateName(record) {
    const metadata = record.draft_metadata || record.current_metadata || record.original_metadata || {};
    const leaf = String(record.relative_root || '').split('/').filter(Boolean).pop();
    return metadataValue(record, 'name') || metadataValue(metadata, 'name') || leaf || '未命名数据集';
  }
  function initialMetadata(record) {
    const result = {};
    const hasReviewedChinese = Boolean(record.current_metadata && typeof record.current_metadata === 'object');
    METADATA_FIELDS.forEach(field => {
      const draft = metadataValue(record.draft_metadata, field);
      const current = metadataValue(record.current_metadata, field);
      const original = metadataValue(record.original_metadata, field);
      if (EVIDENCE_FIELDS.includes(field)) result[field] = original;
      else if (hasReviewedChinese) result[field] = draft || current || '';
      else result[field] = '';
    });
    if (record.readme_managed) {
      result.description = metadataValue(record.original_metadata, 'description');
      result.name = result.name || metadataValue(record.original_metadata, 'name');
    }
    return result;
  }
  function normalizeReaders(value) {
    if (value === 'platform-users') return value;
    const raw = Array.isArray(value) ? value.map(item => typeof item === 'string' ? item : item?.username).filter(Boolean).join(',') : String(value || '');
    const names = raw.split(/[，,\s]+/).map(name => name.trim()).filter(Boolean);
    return [...new Set(names)];
  }
  function readerText(value) {
    if (value === 'platform-users') return '所有平台用户';
    if (Array.isArray(value) && value.length) return `指定账号：${value.map(item => typeof item === 'string' ? item : item?.username).filter(Boolean).join('、')}`;
    return '尚未设置使用范围';
  }
  function clearPoll() {
    if (state.pollTimer !== null) { clearTimeout(state.pollTimer); state.pollTimer = null; }
  }
  function schedulePoll() {
    clearPoll();
    const counts = state.status?.counts || {};
    const busy = Number(counts.queued || 0) + Number(counts.publishing || 0) > 0 || Boolean(state.status?.active_job);
    if (!state.active || !busy) return;
    state.pollTimer = setTimeout(() => { refresh(false).catch(showFailure); }, 5000);
  }
  function scheduleStatusRefresh() {
    clearPoll();
    if (!state.active) return;
    state.pollTimer = setTimeout(() => { refresh(false).catch(showFailure); }, 5000);
  }
  function showFailure(error) {
    setText(q('#data-management-status'), error?.message || '读取数据集管理状态失败。');
  }
  function setControls() {
    const enabled = usable() && !state.scanInFlight;
    const refresh = q('#data-management-refresh');
    if (refresh) refresh.disabled = !enabled;
    if (!usable()) {
      state.actionsDisabledByService = true;
      q('#data-management-detail-content')?.querySelectorAll?.('[data-data-review-action]').forEach(node => { node.disabled = true; });
    } else if (state.actionsDisabledByService) {
      state.actionsDisabledByService = false;
      if (state.selectedDetail) renderDetail(state.selectedDetail);
    }
  }
  function renderStatus() {
    const node = q('#data-management-status'), status = state.status;
    if (!status) { setText(node, '正在读取数据服务状态…'); return; }
    if (!status.configured) setText(node, '受控数据服务尚未配置，暂不能扫描、审核或登记。');
    else if (!status.worker_online) setText(node, '数据服务暂不可用，审核和登记按钮已暂时禁用。');
    else if (status.last_error || status.error) setText(node, `最近一次处理需要关注：${status.last_error || status.error}`);
    else if (status.last_scan) setText(node, `数据服务可用。最近扫描：${status.last_scan}`);
    else setText(node, '数据服务可用。打开此页时会自动发现新增或变化的数据集。');
    const counts = status.counts || {}, host = q('#data-management-counts');
    setText(q('#data-management-intro'), status.discovery_mode === 'readme'
      ? '从共享目录向下查找 README.md；有 README 的目录作为一个数据集，不再拆分其子目录。审核后自动准备权限并登记；README 备注随扫描自动更新。'
      : '系统会比较数据目录和已登记数据集。这里只显示目录级候选，不会逐个登记训练文件或子文件夹。');
    if (host) {
      host.replaceChildren();
      [['pending', '待审核', Number(counts.pending || 0)], ['queued', '等待登记', Number(counts.queued || 0)], ['publishing', '登记中', Number(counts.publishing || 0)], ['published', '已登记', Number(counts.published || 0)], ['attention', '需要处理', Number(counts.failed || 0) + Number(counts.missing || 0) + Number(counts.error || 0)], ['ignored', '暂不纳入', Number(counts.ignored || 0)]].forEach(([, label, count]) => {
        const item = element('span', 'data-management-count'); setText(item, `${label} ${count}`); host.append(item);
      });
    }
    setControls();
  }
  const tagKey = value => String(value).normalize('NFKC').toLocaleLowerCase('zh-CN');
  const tagSelected = tag => state.tags.some(value => tagKey(value) === tagKey(tag));
  function tagSelectable(tag) {
    const choices = state.page?.selectable_tags || state.page?.available_tags || [];
    return !state.tagsLoading && !state.tagsError && choices.some(value => tagKey(value) === tagKey(tag));
  }
  function selectTag(tag) {
    if (typeof tag !== 'string' || !tag) return;
    const selected = tagSelected(tag);
    if (!selected && !tagSelectable(tag)) return;
    state.tags = selected ? state.tags.filter(value => tagKey(value) !== tagKey(tag)) : [...state.tags, tag];
    state.offset = 0;
    loadCandidates().catch(showFailure);
  }
  function tagButton(tag) {
    const selected = tagSelected(tag), disabled = !selected && !tagSelectable(tag);
    const chip = button(tag, () => selectTag(tag), `dataset-tag${selected ? ' selected' : ''}${disabled ? ' unavailable' : ''}`);
    chip.disabled = disabled;
    chip.dataset.dataManagementTag = tag;
    chip.setAttribute?.('aria-pressed', selected ? 'true' : 'false');
    chip.setAttribute?.('aria-disabled', disabled ? 'true' : 'false');
    chip.title = selected ? '已选中，点击取消' : disabled
      ? (state.tagsLoading ? '正在更新筛选结果，请稍候' : state.tagsError ? '标签读取失败，请刷新后再选' : '当前结果中没有此标签，请先取消其他标签或调整状态、搜索条件')
      : '点击按此标签筛选';
    return chip;
  }
  function renderTags(parent, value, clickable = false) {
    if (!Array.isArray(value) || !value.length) return;
    const host = element('p', 'dataset-tags');
    appendText(host, 'span', '标签：');
    value.filter(tag => typeof tag === 'string' && tag).forEach(tag => {
      const chip = clickable ? tagButton(tag) : element('span', 'dataset-tag');
      if (!clickable) setText(chip, tag);
      host.append(chip);
    });
    parent.append(host);
  }
  function renderTagFilters(result) {
    const host = q('#data-management-tags');
    const mode = q('#data-management-tag-mode');
    const clear = q('#data-management-tag-clear');
    const available = Array.isArray(result?.all_tags) ? result.all_tags : (result?.available_tags || []);
    if (host) {
      host.replaceChildren();
      // Keep unavailable and selected tags visible, instead of losing context.
      const choices = new Map();
      [...available, ...state.tags].filter(tag => typeof tag === 'string' && tag).forEach(tag => { if (!choices.has(tagKey(tag))) choices.set(tagKey(tag), tag); });
      choices.forEach(tag => host.append(tagButton(tag)));
    }
    if (mode) {
      mode.value = state.tagMode; mode.disabled = state.tagsLoading || state.tagsError;
      Array.from(mode.options || mode.querySelectorAll?.('option') || []).forEach(option => {
        option.disabled = option.value !== state.tagMode && state.tags.length > 0 && result?.tag_mode_totals?.[option.value] === 0;
        option.title = option.disabled ? '切换后没有匹配数据集，请先取消部分标签' : '';
      });
    }
    if (clear) clear.disabled = !state.tags.length;
  }
  function readmeInfo(record) {
    if (record.readme) return record.readme;
    const value = metadataValue(record.original_metadata, 'description');
    return {markdown: value, complete: !value.includes('[README 较长，网页仅显示前 16 KiB')};
  }
  function exportEntry(record) {
    return {id: record.candidate_id, name: candidateName(record), path: record.relative_root, tags: record.tags,
      hasReadme: Boolean(record.readme_managed), load: async () => {
        const detail = await api(`/api/data-management/candidates/${encodeURIComponent(record.candidate_id)}`);
        if (record.revision !== undefined && detail.revision !== record.revision) throw new Error('数据集说明刚有更新，请刷新列表后重试导出。');
        return readmeInfo(detail);
      }};
  }
  function candidatePath(filters, offset, limit) {
    return `/api/data-management/candidates?status=${encodeURIComponent(filters.filter)}&query=${encodeURIComponent(filters.query)}&tags=${encodeURIComponent(JSON.stringify(filters.tags))}&tag_mode=${encodeURIComponent(filters.tagMode)}&include_inactive=${filters.includeInactive ? 'true' : 'false'}&offset=${offset}&limit=${limit}`;
  }
  async function exportEntries() {
    const filters = {...state, tags: [...state.tags]}, rows = [], ids = new Set();
    let total = null;
    do {
      const result = await api(candidatePath(filters, rows.length, 100));
      if (!Number.isSafeInteger(result.total) || result.total < 0 || (total !== null && result.total !== total) || result.offset !== rows.length || !Array.isArray(result.items)) throw new Error('筛选结果正在变化，请刷新列表后重试导出。');
      total = result.total;
      if (total > rows.length && !result.items.length) throw new Error('读取筛选结果不完整，请重试导出。');
      for (const record of result.items) {
        if (!record.candidate_id || ids.has(record.candidate_id)) throw new Error('筛选结果正在变化，请刷新列表后重试导出。');
        ids.add(record.candidate_id); rows.push(record);
      }
      if (rows.length > total) throw new Error('筛选结果正在变化，请刷新列表后重试导出。');
    } while (rows.length < total);
    return rows.map(exportEntry);
  }
  function exportButton(parent, record) {
    if (!window.ResearchKitReadmeExport) return;
    const control = button('导出 README.md'), status = element('p', 'data-management-note');
    status.setAttribute?.('aria-live', 'polite');
    control.disabled = !record.readme_managed;
    if (control.disabled) control.title = '此数据集未提供 README.md';
    window.ResearchKitReadmeExport.bind(control, status, async () => [exportEntry(record)]);
    parent.append(control, status);
  }
  function renderReadme(parent, info) {
    if (window.ResearchKitMarkdown?.appendReadme) return window.ResearchKitMarkdown.appendReadme(parent, info);
    markdown(parent, info?.markdown || '', 'dataset-readme');
    if (info?.complete === false) paragraph(parent, '请刷新扫描以补齐完整说明。', 'data-management-warning');
  }
  function readmeDetails(parent, record, preview) {
    const details = element('details', 'readme-expand'), summary = appendText(details, 'summary', '展开完整说明');
    const content = element('div'); details.append(content); parent.append(details);
    let loaded = false, loading = false;
    const load = async () => {
      if (loading || loaded) return;
      loading = true; content.replaceChildren(); paragraph(content, '正在读取完整 README…');
      try {
        const value = await api(`/api/data-management/candidates/${encodeURIComponent(record.candidate_id)}`);
        const info = readmeInfo(value); content.replaceChildren(); renderReadme(content, info); loaded = info.complete !== false;
        if (!loaded) content.append(button('重试读取完整说明', load));
      } catch (error) { content.replaceChildren(); paragraph(content, error.message, 'data-management-error'); content.append(button('重试读取完整说明', load)); }
      finally { loading = false; }
    };
    details.ontoggle = async () => {
      if (details.open) state.readmeOpen.add(record.candidate_id); else state.readmeOpen.delete(record.candidate_id);
      setText(summary, details.open ? '收起完整说明' : '展开完整说明');
      if (preview) preview.hidden = details.open;
      if (details.open) await load();
    };
    if (state.readmeOpen.has(record.candidate_id)) { details.open = true; details.ontoggle(); }
  }
  function renderList(result) {
    const list = q('#data-management-list'), pagination = q('#data-management-pagination');
    if (!list || !pagination) return;
    list.replaceChildren(); pagination.replaceChildren();
    const rows = result?.items || [];
    if (!rows.length) {
      paragraph(list, state.filter === 'pending' ? '没有待审核数据集。系统发现新目录或说明变化后会显示在这里。' : '没有符合当前条件的数据集。', 'data-management-empty');
      return;
    }
    rows.forEach(record => {
      const row = element('article', 'card data-management-card');
      appendText(row, 'h3', candidateName(record));
      paragraph(row, `${KIND_LABELS[record.kind] || '待确认'} · ${STATUS_LABELS[record.status] || record.status || '待确认'}`, 'data-management-state');
      if (record.lifecycle?.state && record.lifecycle.state !== 'active') paragraph(row, `资产状态：${window.ResearchKitLifecycle?.STATE_LABELS?.[record.lifecycle.state] || record.lifecycle.state}。`, 'data-management-note');
      if (record.relative_root) paragraph(row, `目录：${record.relative_root}`, 'path');
      renderTags(row, record.tags, true);
      const metadata = record.draft_metadata || record.current_metadata || record.original_metadata || {};
      const description = metadataValue(record, 'description') || metadataValue(metadata, 'description');
      if (record.readme_managed) {
        appendText(row, 'strong', 'README 说明');
        const preview = element('div', 'readme-preview'); markdown(preview, description, 'dataset-readme'); row.append(preview);
        readmeDetails(row, record, preview);
      } else if (description) paragraph(row, description);
      if (record.kind === 'metadata-change') paragraph(row, '本项只更新说明，不会重新登记数据内容。', 'data-management-note');
      if (record.status === 'withdrawn' || ['data-change', 'missing', 'error'].includes(record.kind)) renderDiagnostic(row, record);
      if (record.source_metadata_changed) paragraph(row, '知识库说明已有更新，请复核下面的中文说明。', 'data-management-warning');
      const controls = element('div', 'actions');
      controls.append(button(record.status === 'withdrawn' ? '查看历史原因' : '展开审核', () => openCandidate(record.candidate_id)));
      exportButton(controls, record);
      row.append(controls); list.append(row);
    });
    const total = Number(result.total || 0), offset = Number(result.offset || 0), limit = Number(result.limit || state.limit);
    if (offset > 0) pagination.append(button('上一页', () => { state.offset = Math.max(0, offset - limit); loadCandidates().catch(showFailure); }));
    if (offset + rows.length < total) pagination.append(button('下一页', () => { state.offset = offset + limit; loadCandidates().catch(showFailure); }));
    if (total) paragraph(pagination, `显示 ${offset + 1}–${Math.min(offset + rows.length, total)}，共 ${total} 项。`, 'data-management-page-note');
  }
  async function loadCandidates() {
    const epoch = ++state.listEpoch;
    state.tagsLoading = true; state.tagsError = false; renderTagFilters(state.page);
    const path = candidatePath(state, state.offset, state.limit);
    try {
      const result = await api(path);
      if (!state.active || epoch !== state.listEpoch) return;
      state.page = result || {}; state.tagsLoading = false; state.tagsError = false;
      renderTagFilters(state.page); renderList(state.page);
    } catch (error) {
      if (state.active && epoch === state.listEpoch) {
        state.tagsLoading = false; state.tagsError = true; renderTagFilters(state.page);
      }
      throw error;
    }
  }
  async function loadStatus() {
    const value = await api('/api/data-management');
    if (!state.active) return;
    state.status = value || {};
    renderStatus();
  }
  async function queueScan(manual = false) {
    if (state.scanInFlight || !usable()) return;
    state.scanInFlight = true; setControls();
    try {
      await post('/api/data-management/scan', {});
      setText(q('#data-management-status'), manual ? '已请求刷新扫描；发现过程在后台进行。' : '正在检查是否有新增或变化的数据集。');
      await refresh(false);
    } catch (error) {
      showFailure(error);
    } finally {
      state.scanInFlight = false; setControls();
    }
  }
  async function refresh(autoScan) {
    try { await loadStatus(); }
    catch (error) { showFailure(error); scheduleStatusRefresh(); return; }
    if (!state.active) return;
    if (!state.status?.configured) {
      clearPoll();
      renderList({items: [], total: 0, offset: 0, limit: state.limit});
      setText(q('#data-management-list'), '数据管理后台尚未安装。请由管理员完成一次安装后，重新打开此页面。');
      q('#data-management-pagination')?.replaceChildren();
      const detail = q('#data-management-detail'); if (detail) detail.hidden = true;
      return;
    }
    if (!state.status?.worker_online) { scheduleStatusRefresh(); return; }
    if (autoScan && !state.autoScanRequested && usable()) {
      state.autoScanRequested = true;
      await queueScan(false);
      return;
    }
    try {
      await loadCandidates();
      if (['queued', 'running'].includes(state.selectedDetail?.access_history?.[0]?.status)) await openCandidate(state.selectedId);
      schedulePoll();
    }
    catch (error) { showFailure(error); scheduleStatusRefresh(); }
  }
  function ensureDraft(record) {
    let draft = state.drafts.get(record.candidate_id);
    if (!draft) {
      const readers = record.readers === 'platform-users' ? record.readers : Array.isArray(record.readers) ? normalizeReaders(record.readers) : 'platform-users';
      draft = {revision: record.revision, metadata: initialMetadata(record), readers, boundaryConfirmed: false};
      state.drafts.set(record.candidate_id, draft);
    }
    return draft;
  }
  function renderOriginal(record, parent) {
    const details = element('details', 'data-management-original');
    const verified = record.sourcewordVerified ?? record.source_word_verified;
    appendText(details, 'summary', record.readme_managed ? '说明出处与登记字段' : `原始英文说明与知识库索引${verified === true ? '（已核验）' : verified === false ? '（待核验）' : ''}`);
    const original = record.original_metadata || {};
    METADATA_FIELDS.forEach(field => {
      const value = metadataValue(original, field);
      if (!value) return;
      if (record.readme_managed && field === 'description') return;
      else paragraph(details, `${FIELD_LABELS[field]}：${value}`);
    });
    const previous = record.previous_original_metadata || {};
    if (record.source_metadata_changed && Object.keys(previous).length) {
      appendText(details, 'h3', '知识库原文变更');
      METADATA_FIELDS.forEach(field => {
        const before = metadataValue(previous, field), after = metadataValue(original, field);
        if (before === after) return;
        const change = element('div', 'data-management-diff-row');
        appendText(change, 'strong', FIELD_LABELS[field]);
        if (record.readme_managed && field === 'description') {
          appendText(change, 'p', '之前：'); markdown(change, before, 'dataset-readme');
          appendText(change, 'p', '现在：'); markdown(change, after, 'dataset-readme');
        } else {
          paragraph(change, `之前：${before || '（无）'}`);
          paragraph(change, `现在：${after || '（无）'}`);
        }
        details.append(change);
      });
    }
    if (!details.children || details.children.length > 1) parent.append(details);
  }
  function renderDifference(record, draft, parent) {
    const before = record.current_metadata || {};
    const changed = EDITABLE_FIELDS.filter(field => !(record.readme_managed && field === 'description') && metadataValue(before, field) !== metadataValue(draft.metadata, field));
    const block = element('section', 'data-management-diff');
    appendText(block, 'h3', record.kind === 'metadata-change' ? '说明变更' : '审核前后的说明');
    if (record.kind === 'metadata-change') paragraph(block, '此候选仅更新说明；通过后不会重新登记数据内容。');
    if (!record.current_metadata) paragraph(block, '这是首次登记。审核通过后会保存下方中文说明。');
    if (!changed.length) paragraph(block, record.source_metadata_changed ? '当前中文草稿与已保存中文说明一致；知识库原文的变化请查看下方折叠证据。' : '当前草稿与已保存说明一致。');
    changed.forEach(field => {
      const item = element('div', 'data-management-diff-row');
      appendText(item, 'strong', FIELD_LABELS[field]);
      paragraph(item, `原说明：${metadataValue(before, field) || '（无）'}`);
      paragraph(item, `审核草稿：${metadataValue(draft.metadata, field) || '（无）'}`);
      block.append(item);
    });
    parent.append(block);
  }
  function renderSavedMetadata(record, parent) {
    const metadata = record.current_metadata || record.draft_metadata || record.original_metadata || {};
    const block = element('section', 'data-management-diff');
    appendText(block, 'h3', '已登记说明');
    // The full README is already rendered above the review/saved metadata.
    EDITABLE_FIELDS.forEach(field => {
      if (record.readme_managed && field === 'description') return;
      const value = metadataValue(metadata, field);
      if (value) paragraph(block, `${FIELD_LABELS[field]}：${value}`);
    });
    if (record.readers) paragraph(block, `使用范围：${readerText(record.readers)}`);
    parent.append(block);
  }
  function formInput(label, value, multiline = false) {
    const host = element('label'); setText(host, label);
    const input = element(multiline ? 'textarea' : 'input'); input.value = value;
    if (!multiline) input.maxLength = 16000;
    host.append(input); return {host, input};
  }
  function renderAccess(record, parent) {
    if (!record.registered_dataset_id) return;
    const block = element('section', 'data-management-access'), history = record.access_history || [], latest = history[0];
    const pending = ['queued', 'running'].includes(latest?.status);
    appendText(block, 'h3', '数据集使用范围');
    paragraph(block, `当前使用范围：${readerText(record.readers)}`);
    if (record.access_error) paragraph(block, record.access_error, 'data-management-error');
    if (pending) paragraph(block, `使用范围修改${latest.status === 'queued' ? '等待生效' : '正在处理'}；目标范围：${readerText(latest.readers)}。以当前使用范围为准。`);
    if (latest?.status === 'failed') paragraph(block, `最近一次授权修改未完成：${latest.error || '请刷新后重试'}。上方显示当前实际范围。`, 'data-management-error');
    const draft = state.accessDrafts.get(record.candidate_id);
    if (!draft) {
      const edit = button('修改使用范围', () => {
        state.accessDrafts.set(record.candidate_id, {token: record.access_token, revision: record.revision,
          readers: record.readers === 'platform-users' ? 'platform-users' : normalizeReaders(record.readers)});
        renderDetail(record);
      });
      edit.dataset.dataReviewAction = 'access-edit';
      edit.disabled = !usable() || !record.access_token || pending || ['queued', 'publishing'].includes(record.status);
      block.append(edit);
    } else {
      const form = element('form', 'data-management-access-form'), choice = element('label'); setText(choice, '新的使用范围');
      form.onsubmit = event => event.preventDefault();
      const select = element('select'); select.dataset.accessControl = 'scope';
      for (const [value, label] of [['platform-users', '所有平台用户'], ['accounts', '指定账号']]) {
        const option = element('option'); option.value = value; setText(option, label); select.append(option);
      }
      const accounts = element('input'); accounts.maxLength = 2000; accounts.placeholder = '例如 ly, test'; accounts.dataset.accessControl = 'accounts';
      select.value = draft.readers === 'platform-users' ? 'platform-users' : 'accounts';
      accounts.value = Array.isArray(draft.readers) ? draft.readers.join(', ') : '';
      const save = button('保存使用范围', () => submitAccess(record, draft, save)); save.dataset.dataReviewAction = 'access-save';
      const stale = draft.token !== record.access_token || draft.revision !== record.revision;
      const update = () => {
        accounts.hidden = select.value !== 'accounts';
        draft.readers = select.value === 'accounts' ? normalizeReaders(accounts.value) : 'platform-users';
        accounts.disabled = select.disabled = Boolean(draft.saving) || !usable() || pending;
        save.disabled = Boolean(draft.saving) || !usable() || pending || stale || ['queued','publishing'].includes(record.status) || (Array.isArray(draft.readers) && !draft.readers.length);
      };
      select.onchange = update; accounts.oninput = update; update(); choice.append(select, accounts); form.append(choice);
      paragraph(form, '只修改访问权限，不复制数据或重建版本。撤销账号后限制其后续读取和运行，已下载副本不能收回；移除 ly 也会限制教师自己运行。');
      if (stale) paragraph(form, '授权或候选已经更新，请重新读取后修改，草稿尚未保存。', 'data-management-warning');
      const cancel = button(stale ? '读取最新使用范围' : '取消修改', () => { state.accessDrafts.delete(record.candidate_id); openCandidate(record.candidate_id); });
      cancel.disabled = Boolean(draft.saving); form.append(save, cancel);
      block.append(form);
    }
    if (history.length) {
      const log = element('details'); appendText(log, 'summary', '最近10次授权修改记录');
      const labels = {queued:'等待生效', running:'正在处理', succeeded:'已生效', failed:'未完成', cancelled:'已取消'};
      history.forEach(item => paragraph(log, `${item.created_at} · ${item.reviewer_username} · ${labels[item.status] || item.status}：${readerText(item.before_readers)} → ${readerText(item.readers)}`));
      block.append(log);
    }
    parent.append(block);
  }
  async function submitAccess(record, draft, save) {
    if (!usable() || save.disabled) return;
    save.disabled = true; draft.saving = true; renderDetail(record);
    const epoch = state.detailEpoch;
    try {
      await post(`/api/data-management/candidates/${encodeURIComponent(record.candidate_id)}/access`,
        {revision: draft.revision, access_token: draft.token, readers: draft.readers});
      state.accessDrafts.delete(record.candidate_id);
      await loadStatus();
      if (state.active && state.selectedId === record.candidate_id && state.detailEpoch === epoch) await openCandidate(record.candidate_id);
      schedulePoll();
    } catch (error) {
      draft.saving = false;
      if (state.active && state.selectedId === record.candidate_id && state.detailEpoch === epoch) {
        renderDetail(record);
        paragraph(q('#data-management-detail-content'), error.message, 'data-management-error');
        q('#data-management-detail-content').append(button('读取最新使用范围', () => { state.accessDrafts.delete(record.candidate_id); openCandidate(record.candidate_id); }));
      }
    }
  }
  function renderDetail(record) {
    const section = q('#data-management-detail'), title = q('#data-management-detail-title'), host = q('#data-management-detail-content');
    if (!section || !title || !host) return;
    section.hidden = false; setText(title, `${record.status === 'withdrawn' ? '历史记录' : '审核'}：${candidateName(record)}`); host.replaceChildren();
    const draft = ensureDraft(record);
    paragraph(host, `${KIND_LABELS[record.kind] || '待确认'} · ${STATUS_LABELS[record.status] || record.status || '待确认'}`, 'data-management-state');
    if (record.relative_root) paragraph(host, `${record.status === 'withdrawn' ? '原候选目录' : '待审核目录'}：${record.relative_root}`, 'path');
    renderTags(host, record.tags, true);
    if (record.readme_managed) { appendText(host, 'h3', 'README 说明'); renderReadme(host, readmeInfo(record)); exportButton(host, record); }
    if (record.readme_managed && record.status !== 'withdrawn') paragraph(host, '数据集边界由 README 标记确定，完整性由维护者负责。审核登记会自动准备所选目录的权限；修改 README 后刷新扫描即可更新备注，无需重新审核。', 'data-management-note');
    renderDiagnostic(host, record);
    if (record.status === 'withdrawn') {
      paragraph(host, '这是已自动收起的未登记候选，不是正在使用的数据集。当前目录划分以最新扫描为准；本记录仅供追溯，不提供登记操作。', 'data-management-note');
      return;
    }
    renderAccess(record, host);
    if (record.kind === 'metadata-change') paragraph(host, '这项变化只涉及说明，不会重新登记数据内容。', 'data-management-note');
    if (record.source_metadata_changed) paragraph(host, '知识库说明已有更新。请确认中文说明是否仍适用。', 'data-management-warning');
    if (record.status === 'published') {
      paragraph(host, '该数据集已登记。这里保留说明和知识库证据供查看，无需再次审核。', 'data-management-note');
      renderSavedMetadata(record, host); renderOriginal(record, host); return;
    }
    renderDifference(record, draft, host);
    const form = element('form', 'data-management-review-form');
    appendText(form, 'h3', record.readme_managed ? '数据说明和使用范围' : '中文说明和使用范围');
    paragraph(form, record.readme_managed ? 'README 备注直接使用文件原文，可为空；在文件中维护后会自动同步。其他字段可按需填写。' : '可按需要修改中文说明。知识库原文保留在下方折叠区，不会被自动覆盖。', 'data-management-note');
    if (!record.current_metadata && !record.readme_managed) paragraph(form, '尚无已审核的中文说明，请将下列文字填写为适合使用者阅读的中文。', 'data-management-warning');
    EDITABLE_FIELDS.forEach(field => {
      if (record.readme_managed && field === 'description') return;
      const control = formInput(FIELD_LABELS[field], metadataValue(draft.metadata, field), ['description', 'usage', 'student_conditions'].includes(field));
      control.input.oninput = () => { draft.metadata[field] = control.input.value; };
      form.append(control.host);
    });
    if (record.kind === 'metadata-change') {
      paragraph(form, `使用范围保持不变：${readerText(record.readers)}`, 'data-management-note');
    } else {
      const audience = element('label'); setText(audience, '使用范围');
      const select = element('select');
      const all = element('option'); all.value = 'platform-users'; setText(all, '所有平台用户'); select.append(all);
      const restricted = element('option'); restricted.value = 'accounts'; setText(restricted, '指定账号'); select.append(restricted);
      const accounts = element('input'); accounts.placeholder = '例如 student_a, student_b'; accounts.maxLength = 2000;
      const existingReaders = draft.readers;
      if (Array.isArray(existingReaders)) { select.value = 'accounts'; accounts.value = existingReaders.join(', '); }
      else select.value = 'platform-users';
      const updateReaders = () => { accounts.hidden = select.value !== 'accounts'; draft.readers = select.value === 'accounts' ? normalizeReaders(accounts.value) : 'platform-users'; };
      select.onchange = updateReaders; accounts.oninput = updateReaders; updateReaders(); audience.append(select, accounts); form.append(audience);
    }
    if (record.kind === 'new' && !record.readme_managed) {
      const boundary = element('label', 'data-management-boundary');
      const checkbox = element('input'); checkbox.type = 'checkbox'; checkbox.checked = Boolean(draft.boundaryConfirmed);
      checkbox.onchange = () => { draft.boundaryConfirmed = checkbox.checked; approve.disabled = !canReview(record, draft); };
      boundary.append(checkbox, document.createTextNode('我确认此目录是一个完整数据集或经过明确界定的子集。'));
      form.append(boundary);
    }
    const controls = element('div', 'actions');
    const approve = button(record.kind === 'metadata-change' ? '审核通过并更新说明' : record.readme_managed ? '审核并登记（自动准备权限）' : '审核通过并登记', () => submitReview(record, draft, 'approve'));
    approve.dataset.dataReviewAction = 'approve'; approve.disabled = !canReview(record, draft);
    const ignore = button('暂不纳入', () => submitReview(record, draft, 'ignore'));
    ignore.dataset.dataReviewAction = 'ignore'; ignore.disabled = !usable() || record.status === 'publishing'; controls.append(approve, ignore);
    if (record.status === 'failed' && !['data-change', 'missing', 'error'].includes(record.kind)) {
      const retry = button('重新处理', () => submitReview(record, draft, 'retry'));
      retry.dataset.dataReviewAction = 'retry'; retry.disabled = !usable(); controls.append(retry);
    }
    form.append(controls); host.append(form); renderOriginal(record, host);
  }
  function canReview(record, draft) {
    return usable() && !['queued','running'].includes(record.access_history?.[0]?.status) && !['data-change', 'missing', 'error'].includes(record.kind) && !['queued', 'publishing', 'published', 'withdrawn'].includes(record.status) && (record.kind !== 'new' || record.readme_managed || Boolean(draft.boundaryConfirmed));
  }
  function discardDraftAndReload(record) {
    const ask = typeof window.confirm === 'function' ? window.confirm('读取最新候选会放弃当前未提交的中文草稿。是否继续？') : true;
    if (!ask) return;
    state.drafts.delete(record.candidate_id); openCandidate(record.candidate_id);
  }
  async function openCandidate(candidateId) {
    if (!candidateId) return;
    state.selectedId = candidateId;
    const epoch = ++state.detailEpoch;
    const host = q('#data-management-detail-content'), section = q('#data-management-detail'), previous = state.selectedDetail;
    if (section) section.hidden = false;
    if (host) { host.replaceChildren(); paragraph(host, '正在读取候选详情…', 'data-management-note'); }
    try {
      const detail = await api(`/api/data-management/candidates/${encodeURIComponent(candidateId)}`);
      if (!state.active || epoch !== state.detailEpoch || state.selectedId !== candidateId) return;
      state.selectedDetail = detail; renderDetail(detail);
      const draft = state.drafts.get(candidateId);
      if (draft && draft.revision !== detail.revision && detail.status !== 'withdrawn') {
        paragraph(host, '候选来源已经更新。当前未提交的草稿仍保留；请核对后读取最新候选并重新审核。', 'data-management-warning');
        host.append(button('读取最新候选并重审', () => discardDraftAndReload(detail)));
      }
    } catch (error) {
      if (epoch === state.detailEpoch && state.selectedId === candidateId) {
        if (previous?.candidate_id === candidateId) { state.selectedDetail = previous; renderDetail(previous); paragraph(host, `未能读取最新候选：${error.message}`, 'data-management-error'); host.append(button('读取最新候选并重审', () => discardDraftAndReload(previous))); }
        else if (host) { host.replaceChildren(); paragraph(host, error.message, 'data-management-error'); }
      }
    }
  }
  async function submitReview(record, draft, decision) {
    if (!usable()) return;
    if (decision === 'approve' && !canReview(record, draft)) return;
    const readers = draft.readers === 'platform-users' ? draft.readers : normalizeReaders(draft.readers);
    if (record.kind !== 'metadata-change' && readers !== 'platform-users' && !readers.length) { paragraph(q('#data-management-detail-content'), '请至少填写一个指定账号，或选择“所有平台用户”。', 'data-management-error'); return; }
    const metadata = {};
    EDITABLE_FIELDS.forEach(field => { metadata[field] = metadataValue(draft.metadata, field); });
    const payload = {revision: draft.revision, decision, metadata};
    if (record.kind !== 'metadata-change') payload.readers = readers;
    const form = q('#data-management-detail-content')?.querySelector?.('.data-management-review-form');
    form?.querySelectorAll?.('[data-data-review-action]').forEach(node => { node.disabled = true; });
    try {
      await post(`/api/data-management/candidates/${encodeURIComponent(record.candidate_id)}/review`, payload);
      state.drafts.delete(record.candidate_id);
      await Promise.all([loadStatus(), loadCandidates()]);
      if (state.selectedId === record.candidate_id) await openCandidate(record.candidate_id);
      schedulePoll();
    } catch (error) {
      paragraph(q('#data-management-detail-content'), error.message, 'data-management-error');
      form?.querySelectorAll?.('[data-data-review-action]').forEach(node => { node.disabled = false; });
    }
  }
  function bindControls() {
    if (state.controlsBound) return;
    state.controlsBound = true;
    const filter = q('#data-management-filter'), query = q('#data-management-query'), refresh = q('#data-management-refresh'), inactive = q('#data-management-include-inactive'), mode = q('#data-management-tag-mode'), clear = q('#data-management-tag-clear');
    if (filter) filter.onchange = () => { state.filter = filter.value; state.offset = 0; loadCandidates().catch(showFailure); };
    if (query) query.oninput = () => { state.query = query.value; state.offset = 0; loadCandidates().catch(showFailure); };
    if (inactive) inactive.onchange = () => { state.includeInactive = Boolean(inactive.checked); state.offset = 0; loadCandidates().catch(showFailure); };
    if (mode) mode.onchange = () => {
      const next = mode.value === 'any' ? 'any' : 'all';
      if (state.tagsLoading || state.tagsError || (state.tags.length && state.page?.tag_mode_totals?.[next] === 0)) { mode.value = state.tagMode; return; }
      state.tagMode = next; state.offset = 0; loadCandidates().catch(showFailure);
    };
    if (clear) clear.onclick = () => { if (!state.tags.length) return; state.tags = []; state.offset = 0; loadCandidates().catch(showFailure); };
    if (refresh) refresh.onclick = () => queueScan(true);
    const exportAll = q('#data-management-export');
    if (exportAll && window.ResearchKitReadmeExport) window.ResearchKitReadmeExport.bind(exportAll, q('#data-management-export-status'), exportEntries, {combined: true});
  }
  function init(context) {
    state.context = context; bindControls();
  }
  function enter() {
    if (!state.context || state.context.getMe?.()?.role !== 'teacher') return;
    state.active = true; refresh(true).catch(showFailure);
  }
  function leave() { state.active = false; clearPoll(); }
  function resetForTest() {
    leave(); state.autoScanRequested = false; state.scanInFlight = false; state.status = null; state.filter = 'pending'; state.query = ''; state.tags = []; state.tagMode = 'all'; state.tagsLoading = false; state.tagsError = false; state.offset = 0; state.page = null; state.selectedId = null; state.selectedDetail = null; state.drafts.clear(); state.accessDrafts.clear(); state.readmeOpen.clear(); state.listEpoch = 0; state.detailEpoch = 0; state.actionsDisabledByService = false; state.includeInactive = false;
  }
  window.ResearchKitDataManager = {init, enter, leave, refresh, openCandidate, submitReview, _test: {state, initialMetadata, normalizeReaders, resetForTest, renderDetail, renderList, renderTagFilters, loadCandidates, selectTag, exportEntries}};
})();
