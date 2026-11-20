(() => {
  'use strict';

  const STATUS_LABELS = {
    open: '待处理',
    in_progress: '处理中',
    resolved: '已处理',
  };
  const POLL_MS = 30_000;

  let config = null;
  const state = {
    active: false,
    pageEpoch: 0,
    listEpoch: 0,
    detailEpoch: 0,
    unreadEpoch: 0,
    unreadTimer: null,
    listPage: 1,
    listStatus: 'all',
    listTotal: 0,
    listPageSize: 20,
    selectedThreadId: null,
    selectedMessages: [],
    selectedHasMore: false,
    selectedNextBeforeId: null,
    composeDraft: {subject: '', body: '', requestId: null, payload: null},
    replyDrafts: new Map(),
  };

  const $ = selector => document.querySelector(selector);
  const text = (node, value) => { node.textContent = value == null ? '' : String(value); };
  const make = (tag, className = '', value = null) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value != null) text(node, value);
    return node;
  };
  const paragraph = (host, value, className = '') => {
    const node = make('p', className, value);
    host.append(node);
    return node;
  };
  const isTeacher = () => {
    const me = config?.getMe?.();
    return me?.role === 'teacher' && me?.username === 'ly';
  };
  const host = () => $('#feedback-root');
  const active = () => Boolean(state.active && host() && !host().hidden);
  const message = value => { const target = $('#feedback-message'); if (target) text(target, value); };

  function formatTime(value) {
    if (!value) return '';
    try {
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      return new Intl.DateTimeFormat('zh-CN', {
        timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', hour12: false,
      }).format(date);
    } catch (_) { return String(value); }
  }

  function replyDraft(threadId) {
    if (!state.replyDrafts.has(threadId)) state.replyDrafts.set(threadId, {body: '', requestId: null, payload: null});
    return state.replyDrafts.get(threadId);
  }

  function clearCompletedDraft(draft, requestId, payload, original) {
    if (draft.requestId !== requestId || draft.payload?.body !== payload.body || (payload.subject != null && draft.payload?.subject !== payload.subject)) return false;
    if (draft.body === original.body && (payload.subject == null || draft.subject === original.subject)) {
      draft.body = ''; if (payload.subject != null) draft.subject = '';
      draft.requestId = null; draft.payload = null;
      return true;
    }
    draft.requestId = null; draft.payload = null;
    return false;
  }

  function requestId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    const part = () => Math.floor(Math.random() * 0x100000000).toString(16).padStart(8, '0');
    return `${part()}-${part().slice(0, 4)}-4${part().slice(1, 4)}-8${part().slice(1, 4)}-${part()}${part().slice(0, 4)}`;
  }

  function button(label, action, className = '') {
    const node = make('button', className, label);
    node.type = 'button';
    node.onclick = action;
    return node;
  }

  function setUnread(value) {
    const badge = $('#feedback-unread');
    if (!badge) return;
    const total = Math.max(0, Number(value || 0));
    text(badge, total ? String(total) : '');
    badge.hidden = total === 0;
  }

  async function refreshUnread() {
    if (!config) return;
    const epoch = ++state.unreadEpoch;
    try {
      const value = await config.api(`/api/feedback?page=1&status=all`);
      if (epoch !== state.unreadEpoch) return;
      setUnread(value?.unread_count);
    } catch (_) {
      // The badge is supplemental. A transient failure must not disrupt the app shell.
    }
  }

  function scheduleUnreadRefresh() {
    if (state.unreadTimer) clearTimeout(state.unreadTimer);
    state.unreadTimer = setTimeout(async () => {
      if (config && !document.hidden) await refreshUnread();
      scheduleUnreadRefresh();
    }, POLL_MS);
  }

  function renderShell() {
    const root = host();
    if (!root) return;
    root.replaceChildren();

    const intro = make('p', '', isTeacher()
      ? '查看用户反馈，更新处理状态并在同一会话中回复。'
      : '使用中遇到问题可发给管理员。回复会显示在同一会话中。');
    const notice = make('p', 'action-result'); notice.id = 'feedback-message';

    const compose = make('section', 'card');
    compose.append(make('h2', '', '发送反馈'));
    const form = make('form', 'asset-review-form'); form.id = 'feedback-compose-form';
    const subjectLabel = make('label', '', '主题');
    const subject = make('input'); subject.id = 'feedback-subject'; subject.required = true; subject.maxLength = 120; subject.placeholder = '例如：运行项目时出现错误'; subject.value = state.composeDraft.subject; subject.oninput = () => { state.composeDraft.subject = subject.value; }; subjectLabel.append(subject);
    const bodyLabel = make('label', '', '内容');
    const body = make('textarea'); body.id = 'feedback-body'; body.required = true; body.maxLength = 20000; body.rows = 6; body.placeholder = '请说明出现问题的操作、提示和期望结果。'; body.value = state.composeDraft.body; body.oninput = () => { state.composeDraft.body = body.value; }; bodyLabel.append(body);
    const send = make('button', '', '发送给管理员'); send.type = 'submit'; send.id = 'feedback-send';
    form.append(subjectLabel, bodyLabel, send);
    form.onsubmit = event => submitThread(event, form, subject, body, send);
    compose.append(form);

    const threads = make('section', 'card');
    const threadHeader = make('div', 'actions');
    threadHeader.append(make('h2', '', isTeacher() ? '全部反馈' : '我的反馈'));
    const filter = make('select'); filter.id = 'feedback-status-filter';
    [['all', '全部状态'], ['open', '待处理'], ['in_progress', '处理中'], ['resolved', '已处理']].forEach(([value, label]) => {
      const option = make('option', '', label); option.value = value; option.selected = state.listStatus === value; filter.append(option);
    });
    filter.value = state.listStatus;
    filter.onchange = () => { state.listStatus = filter.value || 'all'; state.listPage = 1; loadThreads(true); };
    threadHeader.append(filter);
    threadHeader.append(button('刷新列表', () => loadThreads(true)));
    const list = make('div', 'cards'); list.id = 'feedback-thread-list';
    const pagination = make('div', 'actions'); pagination.id = 'feedback-pagination';
    threads.append(threadHeader, list, pagination);

    const detail = make('section', 'card'); detail.id = 'feedback-detail'; detail.hidden = true;
    root.append(intro, notice, compose, threads, detail);
  }

  function formatThread(thread) {
    const node = make('article', 'card feedback-thread');
    const open = button(thread.subject || '无主题反馈', () => openThread(thread.thread_id));
    open.className = 'feedback-thread-open';
    node.append(open);
    const owner = isTeacher() ? ` · 用户：${thread.owner_username || '未知'}` : '';
    paragraph(node, `${STATUS_LABELS[thread.status] || thread.status || '待处理'}${owner} · ${formatTime(thread.updated_at || thread.created_at)}`, 'path');
    if (thread.last_preview) paragraph(node, thread.last_preview, 'feedback-preview');
    if (Number(thread.unread_count || 0) > 0) paragraph(node, `未读 ${Number(thread.unread_count)} 条`, 'asset-lifecycle-warning');
    return node;
  }

  function renderThreadList(value) {
    const list = $('#feedback-thread-list');
    const pagination = $('#feedback-pagination');
    if (!list || !pagination) return;
    list.replaceChildren();
    pagination.replaceChildren();
    const rows = Array.isArray(value?.threads) ? value.threads : [];
    state.listPage = Math.max(1, Number(value?.page || state.listPage || 1));
    state.listPageSize = Math.max(1, Number(value?.page_size || state.listPageSize || 20));
    state.listTotal = Math.max(0, Number(value?.total || 0));
    if (!rows.length) paragraph(list, isTeacher() ? '暂时没有用户反馈。' : '你还没有发送反馈。');
    rows.forEach(thread => list.append(formatThread(thread)));
    if (state.listPage > 1) pagination.append(button('上一页', () => { state.listPage -= 1; loadThreads(true); }));
    if (state.listPage * state.listPageSize < state.listTotal) pagination.append(button('下一页', () => { state.listPage += 1; loadThreads(true); }));
    if (state.listTotal > state.listPageSize) paragraph(pagination, `第 ${state.listPage} 页，共 ${state.listTotal} 条。`, 'path');
  }

  async function loadThreads(manual = false) {
    if (!config || !active()) return;
    const epoch = ++state.listEpoch;
    if (manual) message('正在刷新反馈列表…');
    try {
      const value = await config.api(`/api/feedback?page=${encodeURIComponent(state.listPage)}&status=${encodeURIComponent(state.listStatus)}`);
      if (!active() || epoch !== state.listEpoch) return;
      renderThreadList(value || {});
      setUnread(value?.unread_count);
      if (manual) message('反馈列表已刷新。');
    } catch (error) {
      if (active() && epoch === state.listEpoch) message(`无法读取反馈列表：${error.message || '请求失败'}`);
    }
  }

  function ensureLength(value, max, label) {
    if (String(value || '').length > max) throw new Error(`${label}不能超过 ${max} 个字符。`);
  }

  async function submitThread(event, form, subject, body, send) {
    event.preventDefault();
    if (!config || !active() || send.disabled) return;
    const draft = state.composeDraft;
    draft.subject = subject.value; draft.body = body.value;
    const original = {subject: draft.subject, body: draft.body};
    const title = subject.value.trim(), content = body.value.trim();
    try {
      if (!title) throw new Error('请填写反馈主题。');
      if (!content) throw new Error('请填写反馈内容。');
      ensureLength(title, 120, '主题'); ensureLength(content, 20000, '内容');
    } catch (error) { message(error.message); return; }
    const token = state.pageEpoch;
    const payload = {subject: title, body: content};
    const id = draft.requestId && draft.payload?.subject === title && draft.payload?.body === content
      ? draft.requestId : requestId();
    draft.requestId = id; draft.payload = payload;
    send.disabled = true; text(send, '正在发送…');
    try {
      const value = await config.post('/api/feedback', {...payload, request_id: id});
      const cleared = clearCompletedDraft(draft, id, payload, original);
      // The user may have left and re-entered while this request was pending.
      // Update the current form too, but never erase newer text.
      const liveSubject = $('#feedback-subject'), liveBody = $('#feedback-body');
      if (cleared && liveSubject?.value === original.subject && liveBody?.value === original.body) {
        liveSubject.value = ''; liveBody.value = '';
      }
      if (!active() || token !== state.pageEpoch) return;
      if (cleared) { subject.value = ''; body.value = ''; }
      text(send, '发送给管理员');
      message('反馈已发送给管理员。');
      await loadThreads();
      if (value?.thread_id) await openThread(value.thread_id);
    } catch (error) {
      if (active() && token === state.pageEpoch) {
        text(send, '重试发送');
        message(`发送未完成：${error.message || '请求失败'}。内容已保留，可重试。`);
      }
    } finally {
      if (active() && token === state.pageEpoch) send.disabled = false;
    }
  }

  function renderMessage(entry) {
    const node = make('article', 'card feedback-message');
    paragraph(node, `${entry.author_username || '未知用户'} · ${entry.author_role === 'teacher' ? '管理员' : '用户'} · ${formatTime(entry.created_at)}`, 'path');
    const body = make('pre', 'feedback-message-body'); text(body, entry.body || ''); node.append(body);
    return node;
  }

  function renderStatusControls(thread, detail) {
    if (!isTeacher()) return;
    const label = make('label', '', '处理状态');
    const select = make('select');
    ['open', 'in_progress', 'resolved'].forEach(status => {
      const option = make('option', '', STATUS_LABELS[status]); option.value = status; option.selected = thread.status === status; select.append(option);
    });
    const apply = button('更新状态', async () => {
      apply.disabled = select.disabled = true;
      const token = state.pageEpoch, selected = state.selectedThreadId;
      try {
        const value = await config.post(`/api/feedback/${encodeURIComponent(thread.thread_id)}/status`, {status: select.value});
        if (!active() || token !== state.pageEpoch || selected !== state.selectedThreadId) return;
        message(`已更新为${STATUS_LABELS[value?.status] || STATUS_LABELS[select.value]}。`);
        await loadThreads();
        await openThread(thread.thread_id);
      } catch (error) {
        if (active() && token === state.pageEpoch && selected === state.selectedThreadId) {
          message(`更新状态未完成：${error.message || '请求失败'}`);
          apply.disabled = select.disabled = false;
        }
      }
    });
    label.append(select); detail.append(label, apply);
  }

  function renderReplyForm(thread, detail) {
    const draft = replyDraft(thread.thread_id);
    const heading = make('h3', '', '回复');
    const form = make('form', 'asset-review-form');
    const label = make('label', '', '回复内容');
    const body = make('textarea'); body.id = 'feedback-reply-body'; body.required = true; body.maxLength = 20000; body.rows = 5; body.placeholder = '输入回复内容'; body.value = draft.body; body.oninput = () => { draft.body = body.value; }; label.append(body);
    const send = make('button', '', '发送回复'); send.type = 'submit';
    form.append(label, send);
    form.onsubmit = event => submitReply(event, thread.thread_id, body, send);
    detail.append(heading, form);
  }

  async function submitReply(event, threadId, body, send) {
    event.preventDefault();
    if (!config || !active() || state.selectedThreadId !== threadId || send.disabled) return;
    const draft = replyDraft(threadId);
    draft.body = body.value;
    const original = {body: draft.body};
    const content = body.value.trim();
    try {
      if (!content) throw new Error('请填写回复内容。');
      ensureLength(content, 20000, '回复内容');
    } catch (error) { message(error.message); return; }
    const token = state.pageEpoch, detailEpoch = state.detailEpoch;
    const payload = {body: content};
    const id = draft.requestId && draft.payload?.body === content ? draft.requestId : requestId();
    draft.requestId = id; draft.payload = payload;
    send.disabled = true; text(send, '正在发送…');
    try {
      await config.post(`/api/feedback/${encodeURIComponent(threadId)}/reply`, {...payload, request_id: id});
      const cleared = clearCompletedDraft(draft, id, payload, original);
      const liveReply = $('#feedback-reply-body');
      if (cleared && state.selectedThreadId === threadId && liveReply?.value === original.body) liveReply.value = '';
      if (!active() || token !== state.pageEpoch || detailEpoch !== state.detailEpoch || state.selectedThreadId !== threadId) return;
      if (cleared) body.value = ''; text(send, '发送回复');
      message('回复已发送。');
      await loadThreads();
      await openThread(threadId);
    } catch (error) {
      if (active() && token === state.pageEpoch && detailEpoch === state.detailEpoch && state.selectedThreadId === threadId) {
        text(send, '重试发送');
        message(`回复未发送：${error.message || '请求失败'}。内容已保留，可重试。`);
      }
    } finally {
      if (active() && token === state.pageEpoch && detailEpoch === state.detailEpoch && state.selectedThreadId === threadId) send.disabled = false;
    }
  }

  function markRead(threadId, messageId, token, detailEpoch) {
    if (!messageId || !active() || token !== state.pageEpoch || detailEpoch !== state.detailEpoch || state.selectedThreadId !== threadId) return;
    config.post(`/api/feedback/${encodeURIComponent(threadId)}/read`, {message_id: messageId})
      .then(value => {
        if (!active() || token !== state.pageEpoch || detailEpoch !== state.detailEpoch || state.selectedThreadId !== threadId) return;
        if (value && Object.prototype.hasOwnProperty.call(value, 'unread_count')) setUnread(value.unread_count);
        else refreshUnread();
      })
      .catch(() => { /* Reading must remain usable if the optional acknowledgement fails. */ });
  }

  function renderThreadDetail(value, appendOlder = false) {
    const detail = $('#feedback-detail');
    if (!detail || !value?.thread) return;
    const thread = value.thread;
    if (!appendOlder) {
      state.selectedMessages = Array.isArray(value.messages) ? value.messages.slice() : [];
    } else {
      const old = Array.isArray(value.messages) ? value.messages : [];
      const known = new Set(state.selectedMessages.map(item => item.message_id));
      state.selectedMessages = [...old.filter(item => !known.has(item.message_id)), ...state.selectedMessages];
    }
    state.selectedHasMore = Boolean(value.has_more);
    state.selectedNextBeforeId = value.next_before_id || null;
    detail.hidden = false; detail.replaceChildren();
    detail.append(make('h2', '', thread.subject || '无主题反馈'));
    detail.append(button('刷新对话', () => openThread(thread.thread_id)));
    paragraph(detail, `${STATUS_LABELS[thread.status] || thread.status || '待处理'}${isTeacher() ? ` · 用户：${thread.owner_username || '未知'}` : ''} · 创建：${formatTime(thread.created_at)}`, 'path');
    renderStatusControls(thread, detail);
    const messages = make('div', 'cards'); messages.id = 'feedback-thread-messages';
    if (state.selectedHasMore) {
      const older = button('加载更早消息', () => loadOlder(thread.thread_id));
      messages.append(older);
    }
    state.selectedMessages.forEach(entry => messages.append(renderMessage(entry)));
    if (!state.selectedMessages.length) paragraph(messages, '这个会话暂时没有消息。');
    detail.append(messages);
    renderReplyForm(thread, detail);
  }

  async function openThread(threadId) {
    if (!config || !active() || !threadId) return;
    const token = state.pageEpoch, epoch = ++state.detailEpoch;
    state.selectedThreadId = threadId;
    const detail = $('#feedback-detail');
    if (detail) { detail.hidden = false; detail.replaceChildren(make('p', '', '正在读取会话…')); }
    try {
      const value = await config.api(`/api/feedback/${encodeURIComponent(threadId)}`);
      if (!active() || token !== state.pageEpoch || epoch !== state.detailEpoch || state.selectedThreadId !== threadId) return;
      renderThreadDetail(value);
      const lastShown = state.selectedMessages[state.selectedMessages.length - 1];
      markRead(threadId, lastShown?.message_id, token, epoch);
    } catch (error) {
      if (active() && token === state.pageEpoch && epoch === state.detailEpoch && state.selectedThreadId === threadId) message(`无法读取会话：${error.message || '请求失败'}`);
    }
  }

  async function loadOlder(threadId) {
    if (!state.selectedNextBeforeId || state.selectedThreadId !== threadId || !active()) return;
    const token = state.pageEpoch, epoch = state.detailEpoch, beforeId = state.selectedNextBeforeId;
    try {
      const value = await config.api(`/api/feedback/${encodeURIComponent(threadId)}?before_id=${encodeURIComponent(beforeId)}`);
      if (!active() || token !== state.pageEpoch || epoch !== state.detailEpoch || state.selectedThreadId !== threadId) return;
      renderThreadDetail(value, true);
    } catch (error) {
      if (active() && token === state.pageEpoch && epoch === state.detailEpoch && state.selectedThreadId === threadId) message(`无法加载更早消息：${error.message || '请求失败'}`);
    }
  }

  function configure(value) {
    config = value;
    refreshUnread();
    scheduleUnreadRefresh();
  }

  function enter() {
    if (!config || !host()) return;
    state.active = true; state.pageEpoch += 1; state.listEpoch += 1; state.detailEpoch += 1;
    state.selectedThreadId = null; state.selectedMessages = []; state.selectedHasMore = false; state.selectedNextBeforeId = null;
    renderShell();
    loadThreads();
    refreshUnread();
  }

  function leave() {
    state.active = false; state.pageEpoch += 1; state.listEpoch += 1; state.detailEpoch += 1; state.selectedThreadId = null;
  }

  window.ResearchKitFeedback = {configure, enter, leave, refreshUnread, refresh: () => loadThreads(true)};
})();
